"""Pin the relationship between a strategy's *catalogue key* and its
agent ``.name`` (TODO item 12).

These are two distinct identifiers on purpose:

  * the **catalogue key** (``reflective``) is role-scoped and is what the
    resolver, ``/strategy``, recipes, ``arc.toml``, ``ARC_STRATEGY_*``,
    and (since item 7) the YAML workflow engine all select by;
  * the agent **``.name``** (``reflective_reviewer``) is the flat
    registry namespace + a package manifest ``provides.agents[].name``,
    so it must be globally unique.

The two diverge systematically, not randomly. This test freezes the
*current* mapping so a rename on either axis is a conscious, reviewed
change instead of a silent footgun. If you add or rename a strategy,
update ``EXPECTED`` here in the same change.
"""

import pytest

from arc.core.strategies import known_roles, list_strategies, resolve_role

pytestmark = pytest.mark.chat


# role -> {catalogue key: expected agent .name}
EXPECTED: dict[str, dict[str, str]] = {
    "ideator": {
        "default": "ideator",
        "constraint_aware": "constraint_aware_ideator",
        "coscientist": "ideator",
    },
    "searcher": {
        "default": "searcher",
        "embeddings": "searcher_embeddings",
        "materials_project": "searcher_materials_project",
        "negative_results": "searcher_negative",
        "github": "searcher_github",
    },
    "planner": {
        "default": "planner",
        "mars_planner": "mars_planner",
        "active_learning": "active_learning_planner",
        "doe_lhs": "doe_lhs",
        "doe_factorial": "doe_factorial",
        "doe_sobol": "doe_sobol",
    },
    "validator": {
        "default": "validator",
        "materials_evaluators": "materials_validator",
        "dry_run": "dry_run_validator",
    },
    "reviewer": {
        "default": "reviewer",
        "reflective": "reflective_reviewer",
        "comparative": "comparative_reviewer",
    },
    "reflector": {
        "default": "reflector",
        "skill_extracting": "skill_extracting_reflector",
        "failure_clustering": "failure_clustering_reflector",
    },
    "optimizer": {
        "default": "optimizer",
        "bayesopt": "optimizer",
        "cmaes": "optimizer",
        "llm_guided": "optimizer",
    },
    "curator": {
        "default": "curator",
    },
    "builder": {
        "default": "builder",
        "codex": "coder",
        "claude_code": "coder",
    },
}


def test_every_catalogue_role_is_covered():
    """If a role appears in the catalogue it must be pinned here."""
    assert set(EXPECTED) == set(known_roles())


def test_every_strategy_key_is_covered():
    """If a role gains a strategy, EXPECTED must gain its key — otherwise
    the new strategy's .name is unpinned and free to drift."""
    for role in known_roles():
        catalogue_keys = {spec.name for spec in list_strategies(role)}
        assert catalogue_keys == set(EXPECTED[role]), (
            f"role {role!r}: catalogue keys {catalogue_keys} != "
            f"pinned keys {set(EXPECTED[role])}"
        )


@pytest.mark.parametrize(
    "role,key,expected_name",
    [
        (role, key, name)
        for role, mapping in EXPECTED.items()
        for key, name in mapping.items()
    ],
)
def test_resolved_class_name_matches_pin(role, key, expected_name):
    cls = resolve_role(role, overrides={role: key})
    assert getattr(cls, "name", None) == expected_name


def test_default_strategy_name_equals_role():
    """The convention for every role's default: .name == role."""
    for role in known_roles():
        cls = resolve_role(role, overrides={role: "default"})
        assert getattr(cls, "name", None) == role
