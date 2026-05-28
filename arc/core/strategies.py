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

import importlib.util
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
                name="materials_project",
                package_dir="arc-materials",
                module_path="agents/mp_searcher.py",
                attr="MaterialsProjectSearcherAgent",
                description=(
                    "Searches the Materials Project (next-gen API) for "
                    "real DFT-computed reference materials matching the "
                    "goal's elements and target property ranges. Requires "
                    "MP_API_KEY in the environment (.env supported)."
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
                name="mars_planner",
                package_dir="arc-mars",
                module_path="agents/mars_planner.py",
                attr="MARSPlannerAgent",
                description="Cost-aware MARS-style planner.",
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
                name="reflective",
                package_dir="arc-mars",
                module_path="agents/reflective_reviewer.py",
                attr="ReflectiveReviewerAgent",
                description="Reviewer that references prior reflections.",
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
            StrategySpec(
                name="codex",
                package_dir="arc-codex",
                module_path="agents/coder.py",
                attr="CodexCoderAgent",
                description=(
                    "Codex-CLI-backed coder. Drives the Codex agentic "
                    "coding loop to author the artifact. Requires the "
                    "codex CLI; selected via /coder codex."
                ),
            ),
            StrategySpec(
                name="claude_code",
                package_dir="arc-claude-code",
                module_path="agents/coder.py",
                attr="ClaudeCodeCoderAgent",
                description=(
                    "Claude-Code-backed coder. Drives the Claude Code "
                    "agentic loop to author the artifact. Requires the "
                    "claude CLI; selected via /coder claude-code."
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
                name="materials_evaluators",
                package_dir="arc-materials",
                module_path="agents/materials_validator.py",
                attr="MaterialsValidatorAgent",
                description=(
                    "Runs the arc-materials evaluators (band gap, formation "
                    "energy, stability, property prediction) on the run's "
                    "outputs and fails when any value is outside its known "
                    "physical range."
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


# ── Lookups ──────────────────────────────────────────────────────────────


def known_roles() -> list[str]:
    """Roles that the resolver knows about."""
    return list(_ROLE_CATALOGUE.keys())


def list_strategies(role: str) -> list[StrategySpec]:
    """Return every registered strategy for ``role``, default first."""
    if role not in _ROLE_CATALOGUE:
        return []
    return list(_ROLE_CATALOGUE[role][1])


def default_strategy(role: str) -> str | None:
    """Name of the default strategy for ``role``, or None if unknown role."""
    if role not in _ROLE_CATALOGUE:
        return None
    return _ROLE_CATALOGUE[role][0]


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
) -> Any:
    """Resolve ``role`` to an importable class.

    Picks the strategy name (see :func:`resolve_strategy_name`), then loads
    the corresponding module from ``arc/packages/<package_dir>/<module_path>``
    and returns the named attribute. Falls back to the bundled default and
    logs a warning if the chosen strategy can't be loaded.
    """
    if role not in _ROLE_CATALOGUE:
        raise KeyError(f"Unknown research role: {role!r}")

    selected = resolve_strategy_name(role, overrides=overrides, config=config)
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


def _find_spec(role: str, name: str) -> StrategySpec | None:
    for spec in _ROLE_CATALOGUE[role][1]:
        if spec.name == name:
            return spec
    return None


def _load_spec(spec: StrategySpec) -> Any:
    """Import the strategy class from its on-disk module path."""
    pkg_root = Path(__file__).resolve().parent.parent / "packages"
    full_path = pkg_root / spec.package_dir / spec.module_path
    if not full_path.exists():
        raise FileNotFoundError(f"Strategy module not found: {full_path}")
    # Use a stable module name so repeated loads hit sys.modules cache.
    pkg_token = spec.package_dir.replace("-", "_")
    mod_name = f"arc_strategies.{pkg_token}.{spec.module_path.replace('/', '.').removesuffix('.py')}"
    if mod_name in sys.modules:
        module = sys.modules[mod_name]
    else:
        loader_spec = importlib.util.spec_from_file_location(mod_name, full_path)
        module = importlib.util.module_from_spec(loader_spec)
        sys.modules[mod_name] = module
        loader_spec.loader.exec_module(module)
    return getattr(module, spec.attr)
