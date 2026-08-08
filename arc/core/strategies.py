"""Strategy resolver — pick which implementation backs each research role.

The research loop calls fixed *roles* — ``ideator``, ``planner``, ``reviewer``,
``optimizer``, etc. — but each role can be backed by more than one
implementation (an LLM proposer, an embedding-based searcher, a genetic
optimizer, a Bayesian-optimization optimizer). This module is the single
lookup point that picks which class to instantiate, driven by:

    1. A runtime override stored on the workflow's memory dict
       (``memory["strategy_overrides"][role]``). Set by the ``/strategy``
       slash command — highest precedence so the user can flip a strategy
       inside a chat session without restarting.
    2. The environment variable ``ARC_STRATEGY_<ROLE>`` (uppercased).
       Useful for CI, smoke tests, and ``ARC_STRATEGY_PLANNER=mars_planner``
       one-liners.
    3. The ``[strategies]`` table in ``arc.toml``. Persistent per project.
    4. The bundled default — what ``load_<role>()`` returned before this
       module existed.

The resolver returns a class (not an instance) so callers can construct it
with whatever context they have. Missing or unknown strategy names log a
warning and fall back to the default — never raise — because a misspelled
strategy should not break the chat loop.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_PACKAGE_STRATEGIES_BOOTSTRAPPED = False


# ── Role catalogue ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategySpec:
    """One pluggable implementation for a role.

    ``path`` is the package-relative dotted path inside ``arc/packages``,
    using the ``arc-<pkg>`` directory naming. ``attr`` names the class.
    """

    name: str
    package_dir: str
    module_path: str
    attr: str
    description: str = ""
    file_path: str | None = None
    aliases: tuple[str, ...] = ()
    backend_label: str | None = None
    callback_profile: str | None = None


# Role → (default strategy name, list of known strategies).
# Adding a new strategy is one line here plus the implementing class.
_ROLE_CATALOGUE: dict[str, tuple[str, tuple[StrategySpec, ...]]] = {
    "ideator": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/ideator.py",
                attr="IdeatorAgent",
                description="LLM ideator with catalog + results lookup.",
            ),
            StrategySpec(
                name="constraint_aware",
                package_dir="arc-sim2l",
                module_path="agents/ideator_constraint_aware.py",
                attr="ConstraintAwareIdeatorAgent",
                description=(
                    "Ideator that reads the active domain's vocabulary "
                    "(e.g. arc-materials/vocabularies/) and splices "
                    "canonical property names + physical ranges + "
                    "suitable simulation methods into the LLM prompt. "
                    "Falls back to the default ideator behaviour when "
                    "the goal has no recognised domain."
                ),
            ),
        ),
    ),
    "searcher": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/searcher.py",
                attr="KeywordSearcherAgent",
                description=(
                    "Keyword search against the sim2l catalog. The historical "
                    "behaviour the ideator had before search became pluggable."
                ),
            ),
            StrategySpec(
                name="embeddings",
                package_dir="arc-sim2l",
                module_path="agents/searcher.py",
                attr="EmbeddingSearcherAgent",
                description=(
                    "Pulls a wider candidate set then re-ranks by cosine "
                    "similarity. Uses the provider's embed() when available, "
                    "otherwise TF vectors."
                ),
            ),
            StrategySpec(
                name="negative_results",
                package_dir="arc-sim2l",
                module_path="agents/searcher_negative.py",
                attr="NegativeResultsSearcherAgent",
                description=(
                    "Surfaces failed prior runs (errors, all-NaN outputs, "
                    "far-from-target) of catalog simulations matching the "
                    "goal, so the ideator can steer away from known-bad "
                    "parameter regions."
                ),
            ),
            StrategySpec(
                name="github",
                package_dir="arc-sim2l",
                module_path="agents/searcher.py",
                attr="GitHubSearcherAgent",
                description=(
                    "Read side of the GitHub backend: lists artifacts "
                    "published to the configured GitHub repo and ranks them "
                    "by keyword overlap with the goal. Requires GITHUB_TOKEN "
                    "+ ARC_GITHUB_REPO; inactive (empty) otherwise."
                ),
            ),
        ),
    ),
    "planner": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/planner.py",
                attr="PlannerAgent",
                description="LLM planner with parameter-sweep fallback.",
            ),
            StrategySpec(
                name="active_learning",
                package_dir="arc-sim2l",
                module_path="agents/planner_active_learning.py",
                attr="ActiveLearningPlannerAgent",
                description=(
                    "Maximin-distance acquisition over the parameter "
                    "constraints. Picks points farthest from prior runs to "
                    "characterise the response surface — use when you want "
                    "to *learn* a system, not steer toward a target."
                ),
            ),
            StrategySpec(
                name="doe_lhs",
                package_dir="arc-sim2l",
                module_path="agents/planner_doe.py",
                attr="LatinHypercubePlannerAgent",
                description=(
                    "Latin hypercube DoE planner. Stratifies each "
                    "numeric parameter into N bins and picks one sample "
                    "per bin. Good general-purpose coverage."
                ),
            ),
            StrategySpec(
                name="doe_factorial",
                package_dir="arc-sim2l",
                module_path="agents/planner_doe.py",
                attr="FractionalFactorialPlannerAgent",
                description=(
                    "2-level fractional factorial DoE planner. Samples "
                    "the parameter-cube corners + centre. Best for "
                    "sensitivity screening."
                ),
            ),
            StrategySpec(
                name="doe_sobol",
                package_dir="arc-sim2l",
                module_path="agents/planner_doe.py",
                attr="SobolPlannerAgent",
                description=(
                    "Sobol low-discrepancy DoE planner. Quasi-random "
                    "points that fill the parameter cube more uniformly "
                    "than LHS in high dimensions."
                ),
            ),
        ),
    ),
    "reviewer": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/reviewer.py",
                attr="ReviewerAgent",
                description="LLM reviewer judging approval + next params.",
            ),
            StrategySpec(
                name="comparative",
                package_dir="arc-sim2l",
                module_path="agents/reviewer_comparative.py",
                attr="ComparativeReviewerAgent",
                description=(
                    "Deterministic reviewer. Grades each run purely by "
                    "relative improvement over the best prior fitness; "
                    "no LLM round-trip. Useful for stub-mode operation, "
                    "benchmarking optimizers, and auto-acceptance pipelines."
                ),
            ),
        ),
    ),
    "reflector": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/reflector.py",
                attr="ReflectorAgent",
                description="LLM reflector extracting lessons from a review.",
            ),
            StrategySpec(
                name="skill_extracting",
                package_dir="arc-sim2l",
                module_path="agents/reflector_skill_extracting.py",
                attr="SkillExtractingReflectorAgent",
                description=(
                    "Default reflector behaviour + writes a markdown skill "
                    "into ~/.sim2l/<session>/skills/learned/ for each "
                    "actionable review so lessons persist past the session."
                ),
            ),
            StrategySpec(
                name="failure_clustering",
                package_dir="arc-sim2l",
                module_path="agents/reflector_failure_clustering.py",
                attr="FailureClusteringReflectorAgent",
                description=(
                    "Default reflector behaviour + groups recent failed or "
                    "far-from-target runs by error signature. Result lands on "
                    "memory['failure_clusters'] so the next reviewer can spot "
                    "systemic failure modes."
                ),
            ),
        ),
    ),
    "curator": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/curator.py",
                attr="CuratorAgent",
                description="Canonicalises output keys against the registry.",
            ),
        ),
    ),
    "builder": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/builder.py",
                attr="Sim2LBuilderAgent",
                description=(
                    "Built-in LLM artifact builder. Generates the "
                    "workflow.py + sim2l.yaml + tests for a plan. No "
                    "external coding CLI required."
                ),
            ),
        ),
    ),
    "validator": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/validator.py",
                attr="NoopValidatorAgent",
                description=(
                    "No-op post-execution validator. Pass-through that "
                    "preserves the legacy behaviour. Swap in a real "
                    "validator (e.g. materials_evaluators) to grade "
                    "outputs against domain-specific physical ranges."
                ),
            ),
            StrategySpec(
                name="dry_run",
                package_dir="arc-sim2l",
                module_path="agents/validator_dry_run.py",
                attr="DryRunValidatorAgent",
                description=(
                    "Domain-neutral output sanity validator. Fails on "
                    "NaN/inf outputs or missing schema-declared keys; "
                    "warns on out-of-bounds values. Works for any "
                    "artifact regardless of domain."
                ),
            ),
        ),
    ),
    "optimizer": (
        "default",
        (
            StrategySpec(
                name="default",
                package_dir="arc-sim2l",
                module_path="agents/optimizer.py",
                attr="GeneticOptimizerAgent",
                description="Genetic algorithm over the artifact input space.",
            ),
            StrategySpec(
                name="bayesopt",
                package_dir="arc-sim2l",
                module_path="agents/optimizer_bayes.py",
                attr="BayesOptOptimizerAgent",
                description=(
                    "Bayesian optimization (GP + LCB) — preferred when each "
                    "simulation call is expensive. Uses scikit-optimize when "
                    "installed, otherwise a random-sample fallback."
                ),
            ),
            StrategySpec(
                name="cmaes",
                package_dir="arc-sim2l",
                module_path="agents/optimizer_cmaes.py",
                attr="CMAESOptimizerAgent",
                description=(
                    "CMA-ES — best for continuous, correlated parameter "
                    "spaces. Uses pycma when installed, otherwise a "
                    "diagonal coordinate-descent fallback."
                ),
            ),
            StrategySpec(
                name="llm_guided",
                package_dir="arc-sim2l",
                module_path="agents/optimizer_llm.py",
                attr="LLMGuidedOptimizerAgent",
                description=(
                    "LLM-guided optimizer. Hands run history + target + "
                    "bounds to the configured provider and asks for "
                    "candidate points. Best when each simulation is "
                    "expensive enough that LLM tokens are cheap by "
                    "comparison. Falls back to Latin hypercube without "
                    "a provider."
                ),
            ),
        ),
    ),
}


# Immutable snapshot of the bundled catalogue taken at import — the baseline
# ``reset_strategy_catalogue()`` restores to. Captured before any package
# bootstrap or ``register_strategy`` call mutates the live dict, so resetting
# drops dynamically-registered strategies (e.g. test scaffolds) without
# losing the built-ins. (``_ROLE_CATALOGUE`` values are tuples → shallow
# copy of the dict is enough.)
_BUNDLED_CATALOGUE: dict[str, tuple[str, tuple[StrategySpec, ...]]] = dict(_ROLE_CATALOGUE)


def reset_strategy_catalogue() -> None:
    """Restore the catalogue to its bundled (import-time) state.

    Drops any strategies added at runtime via :func:`register_strategy`
    (package scaffolds in tests, ad-hoc registrations) and re-arms the
    package bootstrap so legitimately-installed package strategies reload on
    the next resolver use. Used by the test suite to keep the module-global
    catalogue from leaking registrations across tests.
    """
    global _PACKAGE_STRATEGIES_BOOTSTRAPPED
    _ROLE_CATALOGUE.clear()
    _ROLE_CATALOGUE.update(_BUNDLED_CATALOGUE)
    _PACKAGE_STRATEGIES_BOOTSTRAPPED = False


# ── Lookups ──────────────────────────────────────────────────────────────


def known_roles() -> list[str]:
    """Roles that the resolver knows about."""
    return list(_ROLE_CATALOGUE.keys())


def list_strategies(role: str) -> list[StrategySpec]:
    """Return every registered strategy for ``role``, default first."""
    _ensure_package_strategies_loaded()
    if role not in _ROLE_CATALOGUE:
        return []
    return list(_ROLE_CATALOGUE[role][1])


def default_strategy(role: str) -> str | None:
    """Name of the default strategy for ``role``, or None if unknown role."""
    if role not in _ROLE_CATALOGUE:
        return None
    return _ROLE_CATALOGUE[role][0]


def parse_strategy_names(value: str | None) -> list[str]:
    """Split a strategy selector into ordered component names.

    A selector is usually a single name (``"default"``), but roles that can
    sensibly combine strategies may use ``+``, commas, or whitespace as an
    ordered stack separator, e.g. ``"default+embeddings+materials_project"``
    or ``"default embeddings materials_project"``.
    """
    if not value:
        return []
    return [part.strip() for part in re.split(r"[+,\s]+", str(value)) if part.strip()]


def unknown_strategy_names(role: str, value: str | None) -> list[str]:
    """Return component names in ``value`` that are not valid for ``role``."""
    _ensure_package_strategies_loaded()
    if role not in _ROLE_CATALOGUE:
        return parse_strategy_names(value)
    available = {s.name for s in list_strategies(role)}
    return [name for name in parse_strategy_names(value) if name not in available]


def register_strategy(role: str, spec: StrategySpec, *, make_default: bool = False) -> None:
    """Add a new strategy at runtime.

    External packages can call this to advertise an implementation without
    touching this file. ``make_default=True`` shifts the bundled default.
    """
    if role not in _ROLE_CATALOGUE:
        # New role — register with this spec as its only entry + default.
        _ROLE_CATALOGUE[role] = (spec.name, (spec,))
        return
    current_default, current = _ROLE_CATALOGUE[role]
    # Replace if name collides; otherwise append.
    new = tuple(s for s in current if s.name != spec.name) + (spec,)
    _ROLE_CATALOGUE[role] = (
        spec.name if make_default else current_default,
        new,
    )


# ── Resolution ───────────────────────────────────────────────────────────


def resolve_strategy_name(
    role: str,
    *,
    overrides: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Pick the strategy *name* for ``role`` using the four-level precedence.

    Does not import anything — pure lookup. Returns the default name if no
    override matches. The caller passes ``config`` (a parsed ``arc.toml``
    dict) when they have one; this function does *not* re-read the file.
    """
    if role not in _ROLE_CATALOGUE:
        return ""

    # 1. Runtime override (chat /strategy).
    if overrides and role in overrides and overrides[role]:
        return overrides[role]

    # 2. Environment variable.
    env_key = f"ARC_STRATEGY_{role.upper()}"
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val

    # 3. arc.toml [strategies] table.
    if config:
        configured = (config.get("strategies", {}) or {}).get(role)
        if configured:
            return configured

    # 4. Bundled default.
    return _ROLE_CATALOGUE[role][0]


def resolve_role(
    role: str,
    *,
    overrides: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    disabled_packages: set[str] | None = None,
    loaded_packages: set[str] | None = None,
) -> Any:
    """Resolve ``role`` to an importable class.

    Picks the strategy name (see :func:`resolve_strategy_name`), then loads
    the corresponding module from ``arc/packages/<package_dir>/<module_path>``
    and returns the named attribute. Falls back to the bundled default and
    logs a warning if the chosen strategy can't be loaded.

    ``disabled_packages`` (the session's ``/package disable`` set) makes the
    toggle a real runtime filter: a strategy owned by a disabled package is
    refused and the role falls back to a strategy from an enabled package
    (the bundled default lives in ``arc-sim2l``). Without this the toggle
    only relabelled session state — a disabled package's strategies stayed
    selectable (design/todo.md item 4).
    """
    if role not in _ROLE_CATALOGUE:
        raise KeyError(f"Unknown research role: {role!r}")
    _ensure_package_strategies_loaded()

    disabled = disabled_packages or set()
    loaded = loaded_packages

    selected = resolve_strategy_name(role, overrides=overrides, config=config)
    selected_names = parse_strategy_names(selected)
    if len(selected_names) > 1:
        return _resolve_composite_role(
            role, selected_names,
            disabled_packages=disabled,
            loaded_packages=loaded,
        )

    spec = _find_spec(role, selected)
    if spec is None:
        default_name = _ROLE_CATALOGUE[role][0]
        if selected != default_name:
            logger.warning(
                "Unknown strategy %r for role %r — falling back to %r",
                selected, role, default_name,
            )
            spec = _find_spec(role, default_name)
        if spec is None:
            raise RuntimeError(
                f"No strategy registered for role {role!r} (looked for {selected!r} "
                f"and default {default_name!r})"
            )

    # A strategy from a disabled package is not selectable — fall back to the
    # first strategy whose package is enabled (the default unless it too is
    # disabled, in which case the first enabled one in catalogue order).
    if (loaded is not None and spec.package_dir not in loaded) or spec.package_dir in disabled:
        fallback = _first_enabled_spec(role, disabled)
        if fallback is not None and loaded is not None and fallback.package_dir not in loaded:
            fallback = next(
                (s for s in _ROLE_CATALOGUE[role][1]
                 if s.package_dir not in disabled and s.package_dir in loaded),
                None,
            )
        if fallback is None:
            logger.warning(
                "No loaded/enabled fallback strategy for role %r "
                "(loaded=%s, disabled=%s) — using %r anyway.",
                role, sorted(loaded) if loaded is not None else "any", sorted(disabled), spec.name,
            )
        else:
            logger.info(
                "Strategy %r for role %r is unavailable from package %r — "
                "falling back to %r.", spec.name, role, spec.package_dir, fallback.name,
            )
            spec = fallback

    try:
        return _load_spec(spec)
    except Exception as exc:
        default_name = _ROLE_CATALOGUE[role][0]
        if spec.name != default_name:
            logger.warning(
                "Strategy %r for role %r failed to load (%s) — falling back to %r",
                spec.name, role, exc, default_name,
            )
            return _load_spec(_find_spec(role, default_name))
        raise


def _first_enabled_spec(role: str, disabled: set[str]) -> StrategySpec | None:
    """First strategy for ``role`` whose package is not disabled.

    Prefers the catalogue default when its package is enabled, else the first
    enabled strategy in catalogue order. Returns ``None`` if every strategy
    belongs to a disabled package.
    """
    default_name = _ROLE_CATALOGUE[role][0]
    default_spec = _find_spec(role, default_name)
    if default_spec is not None and default_spec.package_dir not in disabled:
        return default_spec
    for spec in _ROLE_CATALOGUE[role][1]:
        if spec.package_dir not in disabled:
            return spec
    return None


def _resolve_composite_role(
    role: str,
    selected_names: list[str],
    *,
    disabled_packages: set[str] | None = None,
    loaded_packages: set[str] | None = None,
) -> Any:
    """Resolve an ordered strategy stack.

    Only roles with an explicit composite implementation are supported. Today
    that is the ``searcher`` role, where multiple independent sources can be
    queried and merged. Other roles still fall back to their single default
    because "run three optimizers/reviewers/planners" needs role-specific
    merge semantics before it can be safe.

    Components owned by a disabled package are dropped from the stack so the
    ``/package disable`` toggle also prunes composite members (item 4).
    """
    disabled = disabled_packages or set()
    loaded = loaded_packages
    default_name = _ROLE_CATALOGUE[role][0]
    valid: list[str] = []
    for name in selected_names:
        spec = _find_spec(role, name)
        if spec is None:
            logger.warning(
                "Unknown strategy %r in composite for role %r — skipping",
                name, role,
            )
            continue
        if spec.package_dir in disabled:
            logger.info(
                "Strategy %r in composite for role %r is from disabled package "
                "%r — dropping from the stack.", name, role, spec.package_dir,
            )
            continue
        if loaded is not None and spec.package_dir not in loaded:
            logger.info(
                "Strategy %r in composite for role %r is from unloaded package "
                "%r — dropping from the stack.", name, role, spec.package_dir,
            )
            continue
        if name not in valid:
            valid.append(name)

    if len(valid) <= 1:
        return resolve_role(
            role, overrides={role: valid[0] if valid else default_name},
            disabled_packages=disabled,
            loaded_packages=loaded,
        )

    base_cls = _composite_base_for_role(role)
    if base_cls is None:
        logger.warning(
            "Composite strategies are not supported for role %r — falling back to %r",
            role, default_name,
        )
        return resolve_role(
            role, overrides={role: default_name}, disabled_packages=disabled,
            loaded_packages=loaded,
        )

    class_name = f"Composite_{role}_" + "_".join(name.replace("-", "_") for name in valid)
    return type(class_name, (base_cls,), {"strategy_names": tuple(valid)})


def _composite_base_for_role(role: str) -> type | None:
    """Return the composite base class for ``role`` (None if unsupported).

    ``searcher`` keeps its dedicated composite in ``searcher.py``; every other
    role with merge semantics lives in ``composites.py`` (todo.md item 5).
    """
    if role == "searcher":
        return _load_spec(StrategySpec(
            name="composite",
            package_dir="arc-sim2l",
            module_path="agents/searcher.py",
            attr="CompositeSearcherAgent",
            description="Ordered composite searcher.",
        ))
    try:
        from arc.packages.arc_sim2l_agents.composites import COMPOSITE_BY_ROLE
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load composite classes (%s)", exc)
        return None
    return COMPOSITE_BY_ROLE.get(role)


def _find_spec(role: str, name: str) -> StrategySpec | None:
    for spec in _ROLE_CATALOGUE[role][1]:
        if spec.name == name:
            return spec
    return None


def _load_spec(spec: StrategySpec) -> Any:
    """Import the strategy class from its on-disk module path.

    Goes through :mod:`arc.core.module_loader` so a file the package loader has
    already imported (via a ``package.yaml`` entrypoint) yields the *same*
    module — and therefore the same class object — rather than a second copy
    under an ``arc_strategies.*`` name.
    """
    pkg_root = Path(__file__).resolve().parent.parent / "packages"
    full_path = (
        Path(spec.file_path) if spec.file_path
        else pkg_root / spec.package_dir / spec.module_path
    )
    if not full_path.exists():
        raise FileNotFoundError(f"Strategy module not found: {full_path}")
    from arc.core.module_loader import load_module_from_path

    # Historical name, used only when this call is the first to load the file.
    pkg_token = spec.package_dir.replace("-", "_")
    module_token = spec.module_path.replace("/", ".").removesuffix(".py")
    mod_name = f"arc_strategies.{pkg_token}.{module_token}"
    return getattr(load_module_from_path(full_path, preferred_name=mod_name), spec.attr)


def _ensure_package_strategies_loaded() -> None:
    """Load package-declared strategies once for direct resolver users.

    Kernel/ResearchWorkflow load packages explicitly. Some tests and helper
    code call ``resolve_role`` directly, so this lightweight bootstrap lets
    installed packages extend the global strategy catalogue without adding
    package names to this module.
    """
    global _PACKAGE_STRATEGIES_BOOTSTRAPPED
    if _PACKAGE_STRATEGIES_BOOTSTRAPPED:
        return
    _PACKAGE_STRATEGIES_BOOTSTRAPPED = True
    try:
        from arc.core.config import filter_package_paths, load_arc_toml, resolve_package_paths
        from arc.core.loader import load_packages
        from arc.core.registry import ComponentRegistry

        config_path, config = load_arc_toml()
        package_config = config.get("packages", {}) if config else {}
        paths = resolve_package_paths(config, config_path) if config else []
        load_packages(filter_package_paths(paths, package_config), ComponentRegistry())
    except Exception as exc:  # noqa: BLE001
        logger.debug("package strategy bootstrap skipped: %s", exc)
