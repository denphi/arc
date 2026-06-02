"""Recipe auto-suggest — match goals against recipe ``triggers:`` blocks.

Recipes opt in to auto-suggestion by declaring a ``triggers:`` block in
their YAML::

    triggers:
      - domains: [materials]
        target_keys: [bandgap_ev, band_gap, formation_energy]

Each trigger is an AND-of-conditions. A recipe matches if *any* of its
trigger entries matches. A recipe with no ``triggers:`` block is never
auto-suggested — recipes opt in.

Walk the catalogue once per goal. Return the first matching recipe.
Never suggest the same recipe twice in one session (track in
``memory["recipe_suggested"]``), never suggest the already-applied
recipe (track via ``memory["active_recipe"]``).

This module does **not** apply the recipe — it only decides what to
offer. The chat layer's prompt-and-apply UX lives in :mod:`arc.chat.loop`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from arc.core.recipes import Recipe, list_recipes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Suggestion:
    """A single recipe suggestion + the trigger that fired."""

    recipe: Recipe
    reason: str


# ── Trigger evaluation ──────────────────────────────────────────────────


def _normalise_list(value: Any) -> list[str]:
    """Coerce a YAML field to a list of lowercased strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.lower()]
    if isinstance(value, (list, tuple)):
        return [str(v).lower() for v in value if isinstance(v, (str, int, float))]
    return []


def _goal_text(goal: Any) -> str:
    """Return the goal's text in a robust way: ResearchGoal, dict, or plain str."""
    if isinstance(goal, str):
        return goal
    if hasattr(goal, "goal"):
        return str(goal.goal or "")
    if isinstance(goal, dict):
        return str(goal.get("goal") or "")
    return ""


def _goal_domain(goal: Any) -> str:
    if hasattr(goal, "domain"):
        return str(goal.domain or "").lower()
    if isinstance(goal, dict):
        return str(goal.get("domain") or "").lower()
    return ""


def _goal_target(goal: Any) -> dict[str, Any]:
    if hasattr(goal, "target"):
        return dict(goal.target or {})
    if isinstance(goal, dict):
        return dict(goal.get("target") or {})
    return {}


def _detect_elements(goal_text: str, memory: dict[str, Any] | None = None) -> set[str]:
    """Detect element symbols through package-declared detectors.

    Domain packages can register detectors in ``provides.detectors``. This
    keeps recipe suggestion from importing a specific package file directly.
    """
    registry = (memory or {}).get("component_registry") if isinstance(memory, dict) else None
    if registry is not None:
        try:
            detector = registry.get_detector("materials_elements")
            return set(detector(goal_text))
        except Exception as exc:  # noqa: BLE001
            logger.debug("element detector unavailable: %s", exc)
    symbols = {
        "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al",
        "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn",
        "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr",
        "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag",
        "Cd", "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce",
        "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm",
        "Yb", "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg",
        "Tl", "Pb", "Bi", "Po", "At", "Rn",
    }
    names = {"silicon": "Si", "germanium": "Ge", "carbon": "C", "oxygen": "O"}
    text = goal_text.lower()
    found = {symbol for name, symbol in names.items() if re.search(rf"\b{name}\b", text)}
    tokens = re.findall(r"\b[A-Z][a-z]?\b", goal_text)
    found.update(t for t in tokens if t in symbols)
    return found


def _latest_output_keys(memory: dict[str, Any]) -> set[str]:
    """Lower-cased output keys from the most recent run, or empty."""
    history = memory.get("run_history") if isinstance(memory, dict) else None
    if not isinstance(history, list) or not history:
        return set()
    last = history[-1]
    if not isinstance(last, dict):
        return set()
    outputs = last.get("outputs") or {}
    if not isinstance(outputs, dict):
        return set()
    return {str(k).lower() for k in outputs.keys()}


def _iteration_count(memory: dict[str, Any]) -> int:
    """How many runs the session has accumulated.

    Prefer the iteration counter recorded on the workflow context
    (``ResearchContext.iteration``), but fall back to the length of
    ``run_history`` so the trigger still works in setups where the
    counter isn't being maintained.
    """
    if not isinstance(memory, dict):
        return 0
    explicit = memory.get("iteration")
    if isinstance(explicit, int) and explicit >= 0:
        return explicit
    history = memory.get("run_history")
    if isinstance(history, list):
        return len(history)
    return 0


def _trigger_matches(
    trigger: dict,
    goal: Any,
    *,
    memory: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Evaluate one trigger entry. Returns ``(matched, reason)``.

    All conditions in a single trigger entry must match (AND). The
    ``reason`` is the first matched condition; we only need one to
    explain to the user why the suggestion fired.

    The ``memory`` arg is the session-memory dict. It's used by
    state-aware triggers (``output_keys``, ``min_iteration``,
    ``requires_provider``) and ignored by the legacy goal-only ones.
    """
    if not isinstance(trigger, dict) or not trigger:
        return False, ""

    memory = memory if isinstance(memory, dict) else {}

    domains = _normalise_list(trigger.get("domains"))
    keywords = _normalise_list(trigger.get("goal_keywords"))
    target_keys = _normalise_list(trigger.get("target_keys"))
    elements = _normalise_list(trigger.get("elements"))
    output_keys = _normalise_list(trigger.get("output_keys"))
    min_iteration = trigger.get("min_iteration")
    requires_provider = bool(trigger.get("requires_provider"))

    reasons: list[str] = []

    if domains:
        if _goal_domain(goal) not in domains:
            return False, ""
        reasons.append(f"domain={_goal_domain(goal)!r}")

    if keywords:
        text = _goal_text(goal).lower()
        hits = [kw for kw in keywords if re.search(rf"\b{re.escape(kw)}\b", text)]
        if not hits:
            return False, ""
        reasons.append(f"goal contains {hits[0]!r}")

    if target_keys:
        active = {k.lower() for k in _goal_target(goal).keys()}
        hits = [tk for tk in target_keys if tk in active]
        if not hits:
            return False, ""
        reasons.append(f"target {hits[0]!r}")

    if elements:
        found = _detect_elements(_goal_text(goal), memory)
        # Element symbols in YAML may be cased ("Si") or lower ("si").
        wanted = {e.title() if len(e) <= 2 else e for e in elements}
        if not (wanted & found):
            return False, ""
        sample = sorted(wanted & found)[0]
        reasons.append(f"element {sample!r}")

    if output_keys:
        # The trigger fires when the most recent run produced any of the
        # listed output keys. Stub before any run exists ⇒ no match.
        produced = _latest_output_keys(memory)
        hits = [k for k in output_keys if k in produced]
        if not hits:
            return False, ""
        reasons.append(f"output {hits[0]!r}")

    if min_iteration is not None:
        # Reject if the YAML value isn't a non-negative integer — bad
        # input shouldn't accidentally suppress every recipe.
        try:
            threshold = int(min_iteration)
        except (TypeError, ValueError):
            logger.warning(
                "Recipe trigger has invalid min_iteration %r (must be an "
                "integer); treating as 0 (no iteration gate)",
                min_iteration,
            )
            threshold = 0
        if threshold > 0:
            current = _iteration_count(memory)
            if current < threshold:
                return False, ""
            reasons.append(f"iteration ≥ {threshold} (now {current})")

    if requires_provider:
        if not memory.get("provider"):
            return False, ""
        reasons.append("LLM provider available")

    if not reasons:
        # Empty trigger entry: nothing to match against → never fires.
        return False, ""

    return True, " + ".join(reasons)


def _recipe_triggers(recipe: Recipe) -> list[dict]:
    """Pull the ``triggers:`` block off the recipe's raw payload.

    The ``Recipe`` dataclass only models the strategies + description,
    so we re-load the raw YAML to read the trigger list. Cheap (recipes
    are tiny) and avoids leaking trigger semantics into the Recipe
    dataclass.
    """
    raw = _raw_recipe_data(recipe)
    if not raw:
        return []
    triggers = raw.get("triggers")
    if isinstance(triggers, list):
        return [t for t in triggers if isinstance(t, dict)]
    return []


def _raw_recipe_data(recipe: Recipe) -> dict | None:
    """Re-load the recipe YAML body to inspect fields the dataclass drops."""
    try:
        import yaml  # type: ignore[import-not-found]
        from pathlib import Path
        from arc.core import recipes as _recipes_mod

        for base in (_recipes_mod._bundled_recipes_dir(), _recipes_mod._user_recipes_dir()):
            candidate = base / f"{recipe.name}.yaml"
            if candidate.exists():
                with candidate.open("r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                if isinstance(data, dict):
                    return data
                break
    except Exception as exc:  # noqa: BLE001
        logger.debug("could not re-read recipe %s for triggers: %s", recipe.name, exc)
    return None


# ── Public API ─────────────────────────────────────────────────────────


def suggest_recipe(goal: Any, memory: dict[str, Any]) -> Suggestion | None:
    """Pick a recipe to suggest for ``goal``, or ``None`` if none match.

    Walks recipes in catalogue order. Skips recipes that are already
    active or were already suggested in this session.
    """
    if memory is None:
        memory = {}
    active = memory.get("active_recipe")
    suggested_already: list[str] = list(memory.get("recipe_suggested") or [])

    for recipe in list_recipes():
        if recipe.name == active:
            continue
        if recipe.name in suggested_already:
            continue
        for trigger in _recipe_triggers(recipe):
            matched, reason = _trigger_matches(trigger, goal, memory=memory)
            if matched:
                return Suggestion(recipe=recipe, reason=reason)
    return None


def remember_suggestion(memory: dict[str, Any], recipe_name: str) -> None:
    """Record that we've already suggested ``recipe_name`` this session.

    The chat caller invokes this once whether the user accepted or
    declined — we don't want to ask twice either way.
    """
    if memory is None:
        return
    seen: list[str] = list(memory.get("recipe_suggested") or [])
    if recipe_name not in seen:
        seen.append(recipe_name)
        memory["recipe_suggested"] = seen
