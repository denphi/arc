"""Constraint-aware ideator.

Drop-in alternative to the default ``IdeatorAgent``. Same constructor +
same ``run(goal: ResearchGoal) → ResearchProposal`` contract, so the
``ideator`` strategy slot can swap it in without any caller change.

What it adds
============

When ``goal.domain`` matches a package that declares ``vocabularies``
and ``constraints`` in its ``package.yaml`` (today: arc-materials),
the ideator reads those YAML files from disk and splices them into the
LLM prompt. The prompt then asks the LLM to stay inside known physical
ranges, use canonical property names from the vocabulary, and pick
simulation methods that the vocabulary actually lists as suitable for
the target property.

Without a domain-specific vocabulary, behaves exactly like the default
ideator — no regression on non-materials sessions.

Why subclass instead of just editing the default
================================================

The default ideator's catalog-search + LLM-prompt flow is the right
shape; we only need to add one block to the prompt. Subclassing keeps
the search infrastructure shared and means changes to catalog search
benefit both ideators automatically.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from arc.packages.arc_sim2l_agents.ideator import IdeatorAgent
from arc.schemas.research import ResearchGoal, ResearchProposal

logger = logging.getLogger(__name__)


# ── Vocabulary discovery ───────────────────────────────────────────────


def _packages_root() -> Path:
    """``arc/packages`` — where vocabularies live, per package."""
    return Path(__file__).resolve().parent.parent.parent


def _domain_to_package_dir(domain: str | None) -> str | None:
    """Map a goal's ``domain`` string to the on-disk package directory.

    Conservative: we only recognise exact domain ↔ package-name matches
    today (``materials`` → ``arc-materials``). Future domains can opt
    in by editing this map.
    """
    if not isinstance(domain, str) or not domain:
        return None
    return {
        "materials": "arc-materials",
    }.get(domain.lower())


def _load_vocab_yaml(package_dir: str, filename: str) -> dict | None:
    """Load one vocabulary YAML if present; return ``None`` if not.

    Tolerant of: missing PyYAML, missing file, malformed YAML. The
    ideator should never crash because a vocabulary file is missing.
    """
    path = _packages_root() / package_dir / "vocabularies" / filename
    if not path.exists():
        return None
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        logger.debug("PyYAML missing; skipping vocabulary %s", filename)
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to load vocabulary %s: %s", path, exc)
        return None


def _render_vocabulary_block(domain: str, target_keys: list[str]) -> str:
    """Build the prompt block describing the active vocabulary.

    Args:
        domain: the goal's domain (matched to a package dir).
        target_keys: parameter names the user explicitly mentioned as
            targets. Used to highlight the most-relevant vocabulary
            entries so the LLM doesn't drown in 30 properties when
            the goal is about one.
    """
    pkg = _domain_to_package_dir(domain)
    if pkg is None:
        return ""

    properties = _load_vocab_yaml(pkg, "materials_properties.yaml") or {}
    methods = _load_vocab_yaml(pkg, "simulation_methods.yaml") or {}
    if not properties and not methods:
        return ""

    lines: list[str] = []
    lines.append(
        f"Domain vocabulary for ``{domain}`` — use canonical names + "
        f"stay inside the declared ranges:"
    )

    prop_block = properties.get("properties") or {}
    if isinstance(prop_block, dict) and prop_block:
        # Promote any properties the user named as targets to the top
        # so a goal about ``band_gap`` doesn't bury its entry under
        # ``magnetic_moment`` in the prompt.
        target_set = {k.lower().strip() for k in target_keys}
        promoted, demoted = [], []
        for name, spec in prop_block.items():
            (promoted if name.lower() in target_set else demoted).append(
                (name, spec)
            )
        ordered = promoted + demoted
        lines.append("")
        lines.append("Properties (canonical_name: unit, range, [also-known-as]):")
        for name, spec in ordered[:12]:   # cap to keep prompt bounded
            if not isinstance(spec, dict):
                continue
            unit = spec.get("unit", "")
            rng = spec.get("range")
            desc = spec.get("description", "")
            extras = []
            if spec.get("typical_range"):
                extras.append(f"typical={spec['typical_range']}")
            if spec.get("stability_threshold") is not None:
                extras.append(f"stability_threshold={spec['stability_threshold']}")
            extras_str = " · " + " · ".join(extras) if extras else ""
            range_str = f"range={rng}" if rng else ""
            lines.append(
                f"  - {name}: {desc} ({unit}{', ' if range_str else ''}"
                f"{range_str}){extras_str}"
            )

    method_block = methods.get("methods") or {}
    if isinstance(method_block, dict) and method_block:
        # If the user named a target property, highlight methods that
        # are listed as suitable for it. Otherwise list all.
        target_set = {k.lower().strip() for k in target_keys}
        relevant: list[tuple[str, dict]] = []
        other: list[tuple[str, dict]] = []
        for name, spec in method_block.items():
            if not isinstance(spec, dict):
                continue
            suitable_for = {
                str(s).lower() for s in (spec.get("suitable_for") or [])
            }
            bucket = relevant if (suitable_for & target_set) else other
            bucket.append((name, spec))
        ordered_methods = (relevant or other)[:6]
        lines.append("")
        lines.append("Simulation methods (canonical: full_name, suitable_for):")
        for name, spec in ordered_methods:
            full = spec.get("full_name", name)
            suitable = spec.get("suitable_for") or []
            codes = spec.get("typical_codes") or []
            codes_str = f" · codes: {', '.join(codes[:3])}" if codes else ""
            lines.append(
                f"  - {name} ({full}) suitable_for={list(suitable)[:5]}"
                f"{codes_str}"
            )

    lines.append("")
    lines.append(
        "Constraints: prefer canonical names; quantitative claims MUST "
        "include units and fall inside the declared ranges; pick a "
        "simulation method from the list above when the goal calls one out."
    )
    return "\n".join(lines)


# ── Agent ──────────────────────────────────────────────────────────────


class ConstraintAwareIdeatorAgent(IdeatorAgent):
    """Ideator that grounds its prompt in the package's vocabulary.

    Subclasses the default ideator and overrides ``run`` only enough to
    inject the vocabulary block into the prompt's context section. All
    other behaviour (catalog search, prior-results lookup, stub
    fallback) is inherited unchanged.
    """

    name = "constraint_aware_ideator"
    description = (
        "Ideator that reads the active domain's vocabulary (e.g. "
        "arc-materials/vocabularies/) and splices canonical property "
        "names + physical ranges + suitable simulation methods into "
        "the LLM prompt. Falls back to the default ideator behaviour "
        "when the goal has no recognised domain."
    )

    async def run(self, input_data: ResearchGoal) -> ResearchProposal:
        goal = (
            input_data
            if isinstance(input_data, ResearchGoal)
            else ResearchGoal(**input_data)
        )

        # Build the vocabulary block first. When no domain matches the
        # block is empty and we delegate to the parent verbatim, so
        # non-materials sessions see zero behaviour change.
        target_keys = list((goal.target or {}).keys())
        vocab_block = _render_vocabulary_block(goal.domain or "", target_keys)
        if not vocab_block:
            return await super().run(goal)

        # Stash the block on memory before super().run() so the
        # parent's prompt template can consume it. We use a dedicated
        # key (rather than mutating goal.constraints) so the parent's
        # search + history machinery still sees a clean goal.
        memory_key = "_constraint_aware_vocab"
        original = self.context.memory.get(memory_key)
        self.context.memory[memory_key] = vocab_block
        try:
            # Patch the default ideator's prompt-context builder via a
            # contextvar-free shim: override ``catalog_hits`` / etc. is
            # the parent's job; here we simply ask the parent to do
            # everything as usual. The vocab block is consumed by the
            # parent's prompt rendering when ``find_prompt`` returns a
            # template containing the ``{vocabulary}`` placeholder.
            #
            # The default template (materials-hypothesis.md) doesn't
            # have that placeholder yet, so we also append the block
            # to the prompt's context after the parent renders it.
            # See _inject_via_provider below.
            return await self._run_with_vocab_injection(goal, vocab_block)
        finally:
            if original is None:
                self.context.memory.pop(memory_key, None)
            else:
                self.context.memory[memory_key] = original

    async def _run_with_vocab_injection(
        self, goal: ResearchGoal, vocab_block: str,
    ) -> ResearchProposal:
        """Run the parent's pipeline but ensure the vocab block lands
        in front of the LLM.

        We wrap the provider so any ``complete_structured`` call the
        parent makes has the vocab block prepended to its prompt. This
        keeps the wrapper minimal (one call site, one wrapper) and
        avoids re-implementing the parent's whole prompt-construction
        flow.
        """
        provider = self.context.memory.get("provider")
        if provider is None:
            return await super().run(goal)

        wrapped = _VocabWrappedProvider(provider, vocab_block)
        original_provider = provider
        self.context.memory["provider"] = wrapped
        try:
            return await super().run(goal)
        finally:
            # Restore even on exception.
            self.context.memory["provider"] = original_provider


class _VocabWrappedProvider:
    """Thin provider wrapper that prepends a vocab block to every prompt.

    We delegate every attribute access back to the wrapped provider so
    the rest of the chat surface (model name, base_url, etc.) keeps
    working. Only ``complete_structured`` and ``complete`` get the
    vocab treatment.
    """

    def __init__(self, inner: Any, vocab_block: str):
        self._inner = inner
        self._vocab = vocab_block

    async def complete_structured(self, prompt: str, schema, **kwargs):
        return await self._inner.complete_structured(
            self._wrap(prompt), schema, **kwargs,
        )

    async def complete(self, prompt: str, **kwargs):
        return await self._inner.complete(self._wrap(prompt), **kwargs)

    def _wrap(self, prompt: str) -> str:
        return f"{self._vocab}\n\n{prompt}"

    def __getattr__(self, name: str) -> Any:
        # Forward anything else (embed(), model name, etc.) to the
        # underlying provider unchanged.
        return getattr(self._inner, name)
