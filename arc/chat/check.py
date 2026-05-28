"""``arc chat --check`` — config / health dry-run.

Resolves Python env, dotenv, env vars, sim2l services, provider auth,
session dir, packages, skills, and reports a structured verdict.

Design rules:
  * Read-only. Never starts services, never writes files, never sends
    a message to the LLM beyond a single ``list_models`` call.
  * Secrets-safe. Token values are never echoed; only ``set`` / ``unset``.
  * Returns a ``CheckReport`` dataclass — the CLI is responsible for
    rendering (ANSI or JSON).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Verdict = Literal["ok", "warning", "blocked"]


@dataclass(frozen=True)
class CheckItem:
    """One row of the check report."""
    name: str
    verdict: Verdict
    detail: str = ""
    # Free-form structured info (e.g. port, model count). Never includes secrets.
    info: dict = field(default_factory=dict)


@dataclass
class CheckReport:
    """Full report. ``overall`` is the worst row's verdict.

    ``ok < warning < blocked`` — the CLI exits with code 0/1/2.
    """
    items: list[CheckItem] = field(default_factory=list)

    @property
    def overall(self) -> Verdict:
        worst: Verdict = "ok"
        order = {"ok": 0, "warning": 1, "blocked": 2}
        for it in self.items:
            if order[it.verdict] > order[worst]:
                worst = it.verdict
        return worst

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "warning": 1, "blocked": 2}[self.overall]


# ── individual checks ─────────────────────────────────────────────────────

def _check_python_version() -> CheckItem:
    if sys.version_info < (3, 10):
        return CheckItem(
            "Python", "blocked",
            f"requires Python >= 3.10, got {sys.version.split()[0]}",
        )
    return CheckItem("Python", "ok", sys.version.split()[0])


def _check_arc_installed() -> CheckItem:
    try:
        import arc
        version = getattr(arc, "__version__", "unknown")
        return CheckItem("arc package", "ok", str(version))
    except ImportError as exc:
        return CheckItem("arc package", "blocked", f"not importable: {exc}")


def _check_sim2l_installed() -> CheckItem:
    try:
        import sim2l  # noqa: F401
        return CheckItem("sim2l package", "ok", "installed")
    except ImportError:
        return CheckItem("sim2l package", "warning",
                         "not installed — research workflows will skip sim2l")


def _check_dotenv() -> CheckItem:
    """Did dotenv find an ``.env`` file? Don't read its contents."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent.parent / ".env",
    ]
    for p in candidates:
        if p.exists():
            return CheckItem(".env", "ok", str(p))
    return CheckItem(".env", "warning",
                     "no .env file found in cwd or arc/ root — env vars must "
                     "be set in the shell")


def _check_env_vars(provider_hint: str | None) -> list[CheckItem]:
    """For each known auth env var: is it set? Value is NEVER printed.

    When ``provider_hint`` is None and the user has no preference yet, we
    only block on the FIRST provider that has *any* var configured — the
    user clearly intends to use that one. The other providers' missing
    vars become warnings, not blockers.
    """
    items: list[CheckItem] = []
    var_groups = {
        "openwebui": [("OPENWEBUI_KEY", True), ("OPENWEBUI_URL", False),
                      ("OPENWEBUI_MODEL", False)],
        "openai":    [("OPENAI_API_KEY", True), ("OPENAI_MODEL", False)],
        "anthropic": [("ANTHROPIC_API_KEY", True), ("ANTHROPIC_MODEL", False)],
    }

    if provider_hint and provider_hint in var_groups:
        providers = [provider_hint]
    else:
        providers = list(var_groups)

    # Detect which providers have at least one var set when no hint given,
    # so missing required vars on unused providers don't trigger "blocked".
    active_providers: set[str] = set()
    if provider_hint:
        active_providers = {provider_hint}
    else:
        for prov, vars_for_prov in var_groups.items():
            if any(os.environ.get(v) for v, _ in vars_for_prov):
                active_providers.add(prov)
        if not active_providers:
            active_providers = set(providers)  # blocked on the first required

    for provider in providers:
        if provider not in var_groups:
            continue
        is_active = provider in active_providers
        for var, required in var_groups[provider]:
            present = bool(os.environ.get(var))
            if present:
                items.append(CheckItem(f"{var}", "ok", "set"))
            elif required and is_active:
                items.append(CheckItem(f"{var}", "blocked",
                                       f"unset — required for provider '{provider}'"))
            elif required:
                items.append(CheckItem(f"{var}", "warning",
                                       f"unset — only needed if you switch to '{provider}'"))
            else:
                items.append(CheckItem(f"{var}", "warning",
                                       "unset — will use defaults"))
    return items


async def _sim2l_services_report() -> list[CheckItem]:
    """Wrap ``arc.chat.io_utils.check_sim2l_services`` in a CheckItem list.

    Why a separate name from the underlying helper: ``check.py`` returns
    a list of structured ``CheckItem`` rows for the dry-run report,
    while ``io_utils.check_sim2l_services()`` returns the raw
    ``{name: reachable}`` dict the chat banner uses. Same data, different
    shape, deliberately distinct names so a future refactor doesn't
    accidentally collapse them.

    Each probe is a blocking ``requests.get`` with a 1s timeout — we run
    them off the event loop so ``run_check`` stays responsive.
    """
    import asyncio
    from arc.chat.io_utils import check_sim2l_services
    status = await asyncio.to_thread(check_sim2l_services)
    items: list[CheckItem] = []
    for name, up in status.items():
        verdict: Verdict = "ok" if up else "warning"
        detail = "running" if up else "not reachable"
        items.append(CheckItem(f"sim2l {name}", verdict, detail,
                               info={"running": up}))
    return items


def _check_sessions_dir() -> CheckItem:
    from arc.session import sim2l_home
    root = sim2l_home()
    if not root.exists():
        return CheckItem("sessions dir", "ok",
                         f"will create on first session at {root}")
    if not os.access(root, os.W_OK):
        return CheckItem("sessions dir", "blocked",
                         f"{root} is not writable")
    try:
        count = sum(1 for p in root.iterdir() if p.is_dir())
    except OSError as exc:
        return CheckItem("sessions dir", "warning", f"could not enumerate: {exc}")
    return CheckItem("sessions dir", "ok", f"{count} session(s) at {root}",
                     info={"count": count})


def _check_packages() -> CheckItem:
    """Are the package manifests loadable? Catches a common bad-install state."""
    try:
        from arc.packages import (
            load_ideator, load_planner, load_reviewer, load_reflector,
            load_curator, load_builder,
        )
        # Don't actually run them — just import the loaders.
        loaders = [load_ideator, load_planner, load_reviewer,
                   load_reflector, load_curator, load_builder]
        broken = []
        for loader in loaders:
            try:
                loader()
            except Exception as exc:
                broken.append(f"{loader.__name__}: {exc.__class__.__name__}")
        if broken:
            return CheckItem("packages", "warning",
                             f"{len(broken)} loader(s) failed: {', '.join(broken)}")
        return CheckItem("packages", "ok",
                         f"{len(loaders)} agent packages loadable")
    except ImportError as exc:
        return CheckItem("packages", "blocked", f"arc.packages import failed: {exc}")


async def _check_provider_auth(
    provider: str | None,
    token: str | None,
    base_url: str | None,
    model: str | None,
) -> CheckItem:
    """Single billable call: list models. Skipped if no token resolved.

    This is the only network call ``--check`` makes. Cost: typically a
    free metadata request, no LLM inference billed.
    """
    if not provider:
        provider = os.environ.get("ARC_PROVIDER") or "openwebui"

    # Resolve token from env if not passed
    token_resolved = token
    if not token_resolved:
        for env_var in ("OPENWEBUI_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            if os.environ.get(env_var):
                token_resolved = os.environ[env_var]
                break

    if not token_resolved:
        return CheckItem(f"provider {provider}", "warning",
                         "no token — auth skipped (chat will require --stub or --token)")

    # Best effort. Don't fail the whole check if the provider is unreachable.
    try:
        if provider == "openwebui":
            import asyncio
            from arc.providers.openwebui.provider import OpenWebUIProvider
            base = base_url or os.environ.get("OPENWEBUI_URL")
            p = OpenWebUIProvider(base_url=base, token=token_resolved)
            # list_models() does blocking HTTP — run it in a worker thread
            # so we don't block the event loop while the network call sits.
            models = await asyncio.to_thread(p.list_models)
            return CheckItem(f"provider {provider}", "ok",
                             f"{len(models)} models available",
                             info={"model_count": len(models)})
        if provider == "anthropic":
            return CheckItem(f"provider {provider}", "ok",
                             "token present (no list-models probe; SDK doesn't expose one)")
        if provider == "openai":
            return CheckItem(f"provider {provider}", "ok",
                             "token present")
        return CheckItem(f"provider {provider}", "warning",
                         f"unknown provider — skipping probe")
    except Exception as exc:
        return CheckItem(f"provider {provider}", "warning",
                         f"probe failed: {type(exc).__name__}")


# ── public entry point ────────────────────────────────────────────────────

async def run_check(
    provider: str | None = None,
    token: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    probe_provider: bool = True,
) -> CheckReport:
    """Execute all checks and return a structured report.

    Parameters mirror the ``arc chat`` CLI flags so a user can verify
    *exactly* the config they'll launch with.
    """
    report = CheckReport()
    report.items.append(_check_python_version())
    report.items.append(_check_arc_installed())
    report.items.append(_check_sim2l_installed())
    report.items.append(_check_dotenv())
    report.items.extend(_check_env_vars(provider))
    report.items.extend(await _sim2l_services_report())
    report.items.append(_check_sessions_dir())
    report.items.append(_check_packages())
    if probe_provider:
        report.items.append(await _check_provider_auth(provider, token, base_url, model))
    return report
