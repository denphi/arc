"""Recipes — named bundles of research-loop strategy choices.

A *recipe* is a thin YAML file that says "use these strategies together":

    name: bayesian-materials
    description: GP-driven optimisation for materials-science workflows.
    strategies:
      planner:   mars_planner
      reviewer:  reflective
      searcher:  embeddings
      optimizer: bayesopt

Applying a recipe writes its ``strategies`` block into the chat session's
``memory["strategy_overrides"]`` — exactly the dict that
``arc.core.strategies.resolve_strategy_name`` already consults — so we
reuse the existing four-layer precedence (runtime override > env > arc.toml >
default) without inventing a fifth.

Two on-disk locations are scanned:

  1. ``arc/recipes/*.yaml`` — bundled defaults shipped with the package.
  2. ``~/.arc/recipes/*.yaml`` — user-local recipes that override bundled
     ones with the same ``name``. Authoring a project recipe is just a
     YAML file; no Python imports or registration calls needed.

A recipe applied via ``/recipe apply <name>`` does *not* overwrite per-role
``/strategy`` overrides set after the apply — call ``/recipe clear`` to
drop just the recipe-applied keys, leaving any direct ``/strategy`` choices
intact. The bookkeeping for "which keys came from the recipe" lives in
``memory["recipe_applied"]``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Schema ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Recipe:
    """A named bundle of role → strategy choices."""

    name: str
    description: str
    strategies: dict[str, str]
    tags: tuple[str, ...] = ()
    source: str = "bundled"  # "bundled" | "user" — for /recipe list display

    def display_strategies(self) -> str:
        """One-line ``role=impl, role=impl`` summary."""
        return ", ".join(f"{k}={v}" for k, v in self.strategies.items())


@dataclass
class RecipeApplicationResult:
    """What :func:`apply_recipe` did. Returned so the slash command can
    print a useful summary without re-deriving the diff."""

    recipe: Recipe
    overrides_set: dict[str, str] = field(default_factory=dict)
    overrides_skipped: dict[str, str] = field(default_factory=dict)


# ── Discovery ──────────────────────────────────────────────────────────


def _bundled_recipes_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "recipes"


def _user_recipes_dir() -> Path:
    return Path.home() / ".arc" / "recipes"


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("PyYAML missing — recipe %s not loaded", path)
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse recipe %s: %s", path, exc)
        return None


def _recipe_from_dict(data: dict[str, Any], *, source: str) -> Recipe | None:
    """Validate the parsed YAML into a Recipe. Returns ``None`` if invalid.

    A valid recipe has ``name`` (string) and ``strategies`` (string→string
    dict). Description defaults to empty; tags default to empty tuple.
    Unknown extra keys are ignored so future recipe versions can add
    fields without breaking old arc versions.
    """
    name = data.get("name")
    strategies = data.get("strategies") or {}
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(strategies, dict) or not strategies:
        return None
    # Coerce everything to strings for safety.
    clean_strategies: dict[str, str] = {}
    for role, impl in strategies.items():
        if isinstance(role, str) and isinstance(impl, str):
            clean_strategies[role] = impl
        else:
            logger.warning(
                "Recipe %r: dropping invalid strategy entry %r → %r "
                "(both role and impl must be strings)",
                data.get("name", "<unnamed>"), role, impl,
            )
    if not clean_strategies:
        logger.warning(
            "Recipe %r has no valid strategies after filtering — ignoring",
            data.get("name", "<unnamed>"),
        )
        return None
    tags = data.get("tags") or ()
    if isinstance(tags, list):
        tags = tuple(t for t in tags if isinstance(t, str))
    else:
        tags = ()
    return Recipe(
        name=name,
        description=str(data.get("description", "") or ""),
        strategies=clean_strategies,
        tags=tags,
        source=source,
    )


def list_recipes() -> list[Recipe]:
    """Discover all bundled + user recipes. User wins on name collisions.

    Order: user recipes first (so they overshadow bundled ones with the
    same name), then bundled. Within each source, sorted by name for
    stable output.
    """
    found: dict[str, Recipe] = {}

    for dir_path, source in (
        (_bundled_recipes_dir(), "bundled"),
        (_user_recipes_dir(), "user"),
    ):
        if not dir_path.exists():
            continue
        for path in sorted(dir_path.glob("*.yaml")):
            data = _load_yaml(path)
            if data is None:
                continue
            recipe = _recipe_from_dict(data, source=source)
            if recipe is None:
                continue
            # User entries (parsed last) overwrite bundled ones with the
            # same name — exactly what we want for site-local tuning.
            found[recipe.name] = recipe

    return sorted(found.values(), key=lambda r: r.name)


def get_recipe(name: str) -> Recipe | None:
    """Look up a recipe by name. Returns ``None`` if no such recipe."""
    for recipe in list_recipes():
        if recipe.name == name:
            return recipe
    return None


# ── Saving ─────────────────────────────────────────────────────────────


class RecipeSaveError(ValueError):
    """Raised when ``save_recipe`` rejects a request to write a recipe."""


_RECIPE_NAME_RE = __import__("re").compile(r"^[a-z0-9][a-z0-9_\-]{0,62}$")


def _slugify_recipe_name(raw: str) -> str:
    """Normalise an arbitrary string into a recipe-safe slug.

    Lowercases, replaces runs of non-[a-z0-9] with ``-``, strips leading
    and trailing ``-``. Returns ``""`` if nothing usable survives so the
    caller can raise a clean error.
    """
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")
    return slug[:63]


def save_recipe(
    name: str,
    strategies: dict[str, str],
    *,
    description: str = "",
    force: bool = False,
    target_dir: Path | None = None,
) -> Path:
    """Write a recipe YAML under the user-local recipes directory.

    Parameters
    ----------
    name
        User-supplied name. Sanitised to ``[a-z0-9_-]+`` via
        :func:`_slugify_recipe_name`. Empty / unsanitisable input
        raises ``RecipeSaveError``.
    strategies
        ``role → impl`` mapping. Empty mapping is a hard error — we
        won't write a recipe that does nothing. Every pair is checked
        against the strategy catalogue (same logic ``validate_recipe``
        uses) and the function raises on the first invalid pair.
    description
        Optional free-text description embedded in the YAML.
    force
        When ``False`` (default), a name collision with an *existing
        user recipe* raises ``RecipeSaveError``. Bundled recipes can
        never be overwritten regardless of this flag — set ``force``
        only opts into shadowing the bundled one with a user copy.
    target_dir
        For tests: write into a specific directory instead of
        :func:`_user_recipes_dir`. Production callers leave this None.

    Returns the path to the file just written.
    """
    slug = _slugify_recipe_name(name)
    if not slug or not _RECIPE_NAME_RE.fullmatch(slug):
        raise RecipeSaveError(
            f"Invalid recipe name {name!r}: must be lowercase alphanumeric "
            f"with optional ``-`` or ``_``"
        )

    if not strategies:
        raise RecipeSaveError(
            "Cannot save a recipe with no strategies — set at least one "
            "override via /strategy first."
        )

    # Build a transient Recipe so we can reuse validate_recipe()'s
    # catalogue check rather than duplicating it.
    candidate = Recipe(
        name=slug,
        description=description or f"Saved from chat session.",
        strategies=dict(strategies),
        tags=("user-saved",),
        source="user",
    )
    errors = validate_recipe(candidate)
    if errors:
        raise RecipeSaveError(
            f"Cannot save recipe {slug!r}: " + "; ".join(errors)
        )

    out_dir = Path(target_dir) if target_dir is not None else _user_recipes_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    out_path = out_dir / f"{slug}.yaml"
    if out_path.exists() and not force:
        raise RecipeSaveError(
            f"Recipe {slug!r} already exists at {out_path}; pass force=True "
            f"(or use /recipe save … --force) to overwrite."
        )

    # Hand-rolled YAML — keeps PyYAML optional and produces a layout
    # consistent with the bundled recipes (which were also hand-written).
    lines: list[str] = []
    lines.append(f"name: {slug}")
    lines.append(f"description: {_yaml_quote(candidate.description)}")
    lines.append("")
    lines.append("tags:")
    for tag in candidate.tags:
        lines.append(f"  - {tag}")
    lines.append("")
    lines.append("strategies:")
    for role, impl in candidate.strategies.items():
        lines.append(f"  {role}: {impl}")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path


# ── Deletion ────────────────────────────────────────────────────────────


class RecipeDeleteError(ValueError):
    """Raised when ``delete_recipe`` rejects a request.

    Distinct from :class:`RecipeSaveError` so callers (and a future
    ``arc.api`` HTTP layer) can map the two failure modes to different
    status codes if needed.
    """


def delete_recipe(
    name: str,
    *,
    target_dir: Path | None = None,
) -> Path:
    """Remove a user-saved recipe from disk.

    Bundled recipes are read-only — attempting to delete one raises.
    Unknown names raise. The function returns the path that *was*
    removed so the chat can confirm exactly what disappeared.

    Parameters
    ----------
    name
        The recipe name (typically the file stem, e.g. ``my-mp-tuning``).
    target_dir
        For tests: look for the YAML in this directory instead of
        :func:`_user_recipes_dir`. Production callers leave None.
    """
    if not isinstance(name, str) or not name:
        raise RecipeDeleteError("Recipe name must be a non-empty string.")

    # A name we'd reject as a save target is also one we'll never find
    # as a saved recipe — bail early with the same error shape.
    slug = _slugify_recipe_name(name)
    if not slug:
        raise RecipeDeleteError(
            f"No recipe matching {name!r} (name could not be normalised)."
        )

    user_dir = Path(target_dir) if target_dir is not None else _user_recipes_dir()
    candidate = user_dir / f"{slug}.yaml"
    if candidate.exists():
        try:
            candidate.unlink()
        except OSError as exc:
            raise RecipeDeleteError(
                f"Could not remove {candidate}: {exc}"
            ) from exc
        return candidate

    # The recipe wasn't in the user dir — check if it's a bundled one
    # so we can give a useful error instead of a generic "not found".
    bundled = _bundled_recipes_dir() / f"{slug}.yaml"
    if bundled.exists():
        raise RecipeDeleteError(
            f"Refusing to delete bundled recipe {slug!r}: it ships with the "
            f"package and would return on reinstall. To shadow it, save a "
            f"user recipe with the same name (/recipe save {slug} --force)."
        )

    raise RecipeDeleteError(
        f"No user recipe named {slug!r}. List existing recipes with /recipe list."
    )


def _yaml_quote(value: str) -> str:
    """Render a value as a plain or double-quoted YAML scalar.

    We only quote when needed (contains ``:`` or starts with a special
    character) — keeps the rendered file close to the hand-authored
    style of the bundled recipes.
    """
    s = str(value or "")
    needs_quotes = (
        not s
        or any(c in s for c in (":", "#", "\n"))
        or s[:1] in ("&", "*", "?", "|", ">", "!", "%", "@", "`", "'", '"', "-")
    )
    if not needs_quotes:
        return s
    escaped = s.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


# ── Application ────────────────────────────────────────────────────────


def apply_recipe(
    recipe: Recipe,
    memory: dict[str, Any],
    *,
    overwrite_manual: bool = False,
) -> RecipeApplicationResult:
    """Merge a recipe's strategies into ``memory["strategy_overrides"]``.

    ``memory`` is the workflow's session-memory dict (what
    ``ChatState.memory`` points at). The function mutates it in place
    so the resolver picks up the changes on the next call.

    Roles already overridden by a prior ``/strategy <role>`` call are
    skipped unless ``overwrite_manual=True``. We track the recipe's
    contribution in ``memory["recipe_applied"]`` so ``clear_recipe``
    can undo just those keys without disturbing direct overrides.

    Validating each ``(role, impl)`` against the strategy catalogue is
    the caller's responsibility — usually the ``/recipe`` slash command,
    which surfaces a single error to the user instead of half-applying.
    """
    overrides: dict[str, str] = dict(memory.get("strategy_overrides") or {})
    previously_recipe: dict[str, str] = dict(memory.get("recipe_applied") or {})

    # A "manual" override is one in ``strategy_overrides`` that wasn't
    # placed there by the previous recipe.
    set_now: dict[str, str] = {}
    skipped: dict[str, str] = {}

    for role, impl in recipe.strategies.items():
        is_manual = (
            role in overrides
            and overrides[role] != previously_recipe.get(role)
        )
        if is_manual and not overwrite_manual:
            skipped[role] = overrides[role]
            continue
        overrides[role] = impl
        set_now[role] = impl

    # Remove keys from the *prior* recipe that this one doesn't touch —
    # otherwise apply→apply leaks stale roles between recipes.
    for role, impl in previously_recipe.items():
        if role in set_now or role in skipped:
            continue
        # Drop the prior-recipe key from overrides only if it still
        # matches what that recipe put there (i.e. user hasn't replaced
        # it manually since).
        if overrides.get(role) == impl:
            overrides.pop(role, None)

    if overrides:
        memory["strategy_overrides"] = overrides
    else:
        memory.pop("strategy_overrides", None)

    if set_now:
        memory["recipe_applied"] = set_now
    else:
        memory.pop("recipe_applied", None)

    return RecipeApplicationResult(
        recipe=recipe, overrides_set=set_now, overrides_skipped=skipped,
    )


def clear_recipe(memory: dict[str, Any]) -> dict[str, str]:
    """Drop just the override keys placed by the last applied recipe.

    Returns the dict of cleared (role, impl) pairs so the slash command
    can echo what changed. Per-role ``/strategy`` overrides set after
    the recipe was applied are preserved.
    """
    recipe_applied: dict[str, str] = dict(memory.get("recipe_applied") or {})
    if not recipe_applied:
        return {}

    overrides: dict[str, str] = dict(memory.get("strategy_overrides") or {})
    cleared: dict[str, str] = {}
    for role, impl in recipe_applied.items():
        # Only drop if it still matches what we put there.
        if overrides.get(role) == impl:
            overrides.pop(role, None)
            cleared[role] = impl

    if overrides:
        memory["strategy_overrides"] = overrides
    else:
        memory.pop("strategy_overrides", None)
    memory.pop("recipe_applied", None)
    return cleared


# ── Validation helpers ─────────────────────────────────────────────────


def validate_recipe(recipe: Recipe) -> list[str]:
    """Return human-readable errors for a recipe.

    Checks each ``(role, impl)`` against the strategy catalogue and
    returns a list of error strings (empty list = valid). Used by the
    ``/recipe apply`` command to fail fast with a useful message
    instead of silently misrouting at resolve time.
    """
    from arc.core.strategies import known_roles, list_strategies

    errors: list[str] = []
    for role, impl in recipe.strategies.items():
        if role not in known_roles():
            errors.append(f"unknown role: {role!r}")
            continue
        available = {s.name for s in list_strategies(role)}
        if impl not in available:
            errors.append(
                f"unknown strategy {impl!r} for {role}; available: "
                f"{', '.join(sorted(available))}"
            )
    return errors
