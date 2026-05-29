"""HTTP routes for the pluggable research-loop surface.

Same coverage shape as ``test_api_routes.py``: call the route functions
directly (skipping FastAPI's dependency injection) and assert behaviour
+ HTTP status codes for sad paths.

Persisted state lives under ``<SIM2L_HOME>/<session_id>/session_state.json``;
the global pytest fixture redirects ``SIM2L_HOME`` to a temp dir so
nothing leaks into the user's actual session directory.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from arc.api.research_loop_routes import (
    RecipeApplyRequest,
    RecipeSaveRequest,
    SkillTransferRequest,
    StrategyOverrideRequest,
    apply_recipe_endpoint,
    clear_active_recipe_endpoint,
    clear_strategy_endpoint,
    delete_recipe_endpoint,
    delete_skill_endpoint,
    export_skills_endpoint,
    import_skills_endpoint,
    list_clusters_endpoint,
    list_recipes_endpoint,
    list_skills_endpoint,
    list_strategies_endpoint,
    save_recipe_endpoint,
    set_strategy_endpoint,
    show_cluster_endpoint,
    show_recipe_endpoint,
    show_skill_endpoint,
)
from arc.api.session_state import load_state


pytestmark = pytest.mark.chat


def _run(result):
    """Invoke a route handler result, tolerating sync or async handlers.

    These routes were converted from ``async def`` to plain ``def`` (so
    FastAPI runs their blocking file I/O in its threadpool). The tests call
    the handlers directly, so this shim awaits a coroutine if one is
    returned and otherwise passes the value straight through.
    """
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def session_id():
    """Stable per-test session id derived from the conftest tmp home."""
    return "test-api-session"


def _write_skill_file(session_id: str, filename: str, body: str) -> Path:
    base = Path(os.environ["SIM2L_HOME"]) / session_id / "skills" / "learned"
    base.mkdir(parents=True, exist_ok=True)
    path = base / filename
    path.write_text(body, encoding="utf-8")
    return path


# ── /strategies ────────────────────────────────────────────────────────


def test_strategies_list_returns_all_roles(session_id):
    payload = _run(list_strategies_endpoint(session_id=session_id))
    assert payload["session_id"] == session_id
    roles = {r["role"] for r in payload["roles"]}
    # Every role we registered should appear.
    for expected in ("ideator", "planner", "reviewer", "optimizer",
                     "searcher", "validator", "reflector", "curator"):
        assert expected in roles


def test_strategies_list_marks_defaults(session_id):
    payload = _run(list_strategies_endpoint(session_id=session_id))
    planner = next(r for r in payload["roles"] if r["role"] == "planner")
    # No override → active = default; session_override is None.
    assert planner["active"] == planner["default"]
    assert planner["session_override"] is None


def test_strategies_set_persists_override(session_id):
    _run(set_strategy_endpoint(
        role="planner",
        body=StrategyOverrideRequest(impl="mars_planner"),
        session_id=session_id,
    ))
    # Re-read via the list endpoint — the override should be active.
    payload = _run(list_strategies_endpoint(session_id=session_id))
    planner = next(r for r in payload["roles"] if r["role"] == "planner")
    assert planner["active"] == "mars_planner"
    assert planner["session_override"] == "mars_planner"
    # And it survived to disk.
    state = load_state(session_id)
    assert state["strategy_overrides"]["planner"] == "mars_planner"


def test_strategies_set_rejects_unknown_role(session_id):
    with pytest.raises(Exception) as exc:
        _run(set_strategy_endpoint(
            role="not_a_role",
            body=StrategyOverrideRequest(impl="default"),
            session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_strategies_set_rejects_unknown_impl(session_id):
    with pytest.raises(Exception) as exc:
        _run(set_strategy_endpoint(
            role="planner",
            body=StrategyOverrideRequest(impl="not_a_strategy"),
            session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 400


def test_strategies_clear_drops_session_override(session_id):
    # Set then clear.
    _run(set_strategy_endpoint(
        role="optimizer",
        body=StrategyOverrideRequest(impl="bayesopt"),
        session_id=session_id,
    ))
    payload = _run(clear_strategy_endpoint(
        role="optimizer", session_id=session_id,
    ))
    assert payload["cleared"] == "bayesopt"
    state = load_state(session_id)
    assert "optimizer" not in (state.get("strategy_overrides") or {})


def test_strategies_clear_unknown_role_404(session_id):
    with pytest.raises(Exception) as exc:
        _run(clear_strategy_endpoint(
            role="not_a_role", session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_strategies_rejects_bad_session_id():
    with pytest.raises(Exception) as exc:
        _run(list_strategies_endpoint(session_id="../escape"))
    assert getattr(exc.value, "status_code", None) == 400


# ── /recipes ───────────────────────────────────────────────────────────


def test_recipes_list_includes_bundled(session_id):
    payload = _run(list_recipes_endpoint(session_id=session_id))
    names = {r["name"] for r in payload["recipes"]}
    # Five bundled recipes ship today.
    for expected in ("bayesian-materials", "cmaes-continuous",
                     "exploration-baseline", "mp-discovery", "failure-aware"):
        assert expected in names


def test_recipes_show_returns_full_recipe(session_id):
    payload = _run(show_recipe_endpoint(
        name="mp-discovery", session_id=session_id,
    ))
    assert payload["name"] == "mp-discovery"
    assert payload["strategies"]["searcher"] == "materials_project"


def test_recipes_show_unknown_404(session_id):
    with pytest.raises(Exception) as exc:
        _run(show_recipe_endpoint(
            name="not-a-recipe", session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_recipes_apply_writes_overrides_and_active(session_id):
    payload = _run(apply_recipe_endpoint(
        name="bayesian-materials",
        body=RecipeApplyRequest(),
        session_id=session_id,
    ))
    assert payload["applied"] == "bayesian-materials"
    assert payload["active_recipe"] == "bayesian-materials"
    assert payload["strategy_overrides"]["optimizer"] == "bayesopt"
    # Persisted.
    state = load_state(session_id)
    assert state["active_recipe"] == "bayesian-materials"
    assert state["strategy_overrides"]["optimizer"] == "bayesopt"


def test_recipes_apply_unknown_404(session_id):
    with pytest.raises(Exception) as exc:
        _run(apply_recipe_endpoint(
            name="not-a-recipe",
            body=RecipeApplyRequest(),
            session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_recipes_save_round_trip(session_id, tmp_path, monkeypatch):
    # Set up two overrides, then save, then re-list to find the new one.
    from arc.core import recipes as _recipes
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)

    _run(set_strategy_endpoint(
        role="planner",
        body=StrategyOverrideRequest(impl="mars_planner"),
        session_id=session_id,
    ))
    _run(set_strategy_endpoint(
        role="optimizer",
        body=StrategyOverrideRequest(impl="bayesopt"),
        session_id=session_id,
    ))

    saved = _run(save_recipe_endpoint(
        body=RecipeSaveRequest(name="api-saved", description="via API"),
        session_id=session_id,
    ))
    assert saved["saved"] == "api-saved"
    yaml_path = tmp_path / "api-saved.yaml"
    assert yaml_path.exists()
    body = yaml_path.read_text()
    assert "planner: mars_planner" in body
    assert "via API" in body


def test_recipes_save_rejects_when_no_overrides(session_id):
    with pytest.raises(Exception) as exc:
        _run(save_recipe_endpoint(
            body=RecipeSaveRequest(name="empty"),
            session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 400


def test_recipes_delete_rejects_bundled(session_id):
    with pytest.raises(Exception) as exc:
        _run(delete_recipe_endpoint(
            name="bayesian-materials", session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 403


def test_recipes_delete_unknown_404(session_id):
    with pytest.raises(Exception) as exc:
        _run(delete_recipe_endpoint(
            name="never-existed", session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_recipes_delete_user_recipe(session_id, tmp_path, monkeypatch):
    from arc.core import recipes as _recipes
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)
    _recipes.save_recipe(
        "doomed", {"planner": "default"}, target_dir=tmp_path,
    )
    _run(delete_recipe_endpoint(
        name="doomed", session_id=session_id,
    ))
    assert not (tmp_path / "doomed.yaml").exists()


def test_recipes_delete_active_clears_state(session_id, tmp_path, monkeypatch):
    from arc.core import recipes as _recipes
    monkeypatch.setattr(_recipes, "_user_recipes_dir", lambda: tmp_path)
    _recipes.save_recipe(
        "active", {"planner": "mars_planner"}, target_dir=tmp_path,
    )
    _run(apply_recipe_endpoint(
        name="active",
        body=RecipeApplyRequest(),
        session_id=session_id,
    ))
    payload = _run(delete_recipe_endpoint(
        name="active", session_id=session_id,
    ))
    assert payload["cleared_active"] is True
    state = load_state(session_id)
    assert state.get("active_recipe") is None


def test_recipes_clear_drops_active_overrides(session_id):
    _run(apply_recipe_endpoint(
        name="bayesian-materials",
        body=RecipeApplyRequest(),
        session_id=session_id,
    ))
    _run(clear_active_recipe_endpoint(session_id=session_id))
    state = load_state(session_id)
    assert state.get("active_recipe") is None
    assert "strategy_overrides" not in state or not state["strategy_overrides"]


# ── /clusters ──────────────────────────────────────────────────────────


def _write_session_meta(session_id: str, **fields) -> None:
    """Write a fake session.json including failure_clusters."""
    import json
    from arc.session import _session_dir
    path = _session_dir(session_id) / "session.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": session_id, **fields}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_clusters_empty_when_no_session_meta(session_id):
    payload = _run(list_clusters_endpoint(session_id=session_id))
    assert payload["clusters"] == []


def test_clusters_lists_from_session_meta(session_id):
    _write_session_meta(
        session_id,
        failure_clusters=[
            {"signature": "scf-fail", "count": 3, "reason": "SCF",
             "entries": [{}]},
            {"signature": "all-nan", "count": 2, "reason": "NaN",
             "entries": [{}]},
        ],
    )
    payload = _run(list_clusters_endpoint(session_id=session_id))
    assert len(payload["clusters"]) == 2
    assert payload["clusters"][0]["signature"] == "scf-fail"


def test_clusters_show_exact_match(session_id):
    _write_session_meta(
        session_id,
        failure_clusters=[
            {"signature": "x", "count": 2, "reason": "y", "entries": [{}]},
        ],
    )
    payload = _run(show_cluster_endpoint(
        signature="x", session_id=session_id,
    ))
    assert payload["signature"] == "x"


def test_clusters_show_prefix_match(session_id):
    _write_session_meta(
        session_id,
        failure_clusters=[
            {"signature": "long-error-message", "count": 2,
             "reason": "msg", "entries": [{}]},
        ],
    )
    payload = _run(show_cluster_endpoint(
        signature="long-error", session_id=session_id,
    ))
    assert payload["signature"] == "long-error-message"


def test_clusters_show_ambiguous_returns_409(session_id):
    _write_session_meta(
        session_id,
        failure_clusters=[
            {"signature": "a-one", "count": 2, "reason": "x", "entries": []},
            {"signature": "a-two", "count": 1, "reason": "y", "entries": []},
        ],
    )
    with pytest.raises(Exception) as exc:
        _run(show_cluster_endpoint(signature="a", session_id=session_id))
    assert getattr(exc.value, "status_code", None) == 409


def test_clusters_show_missing_404(session_id):
    _write_session_meta(
        session_id,
        failure_clusters=[
            {"signature": "x", "count": 2, "reason": "y", "entries": []},
        ],
    )
    with pytest.raises(Exception) as exc:
        _run(show_cluster_endpoint(
            signature="not-here", session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


# ── /skills ────────────────────────────────────────────────────────────


def test_skills_list_empty_session(session_id):
    payload = _run(list_skills_endpoint(session_id=session_id))
    assert payload["skills"] == []


def test_skills_list_returns_files(session_id):
    _write_skill_file(
        session_id, "first-aaa.md",
        "# learned_skill: first\nbody\n",
    )
    payload = _run(list_skills_endpoint(session_id=session_id))
    assert len(payload["skills"]) == 1
    assert payload["skills"][0]["name"] == "first-aaa"
    assert "learned_skill: first" in payload["skills"][0]["h1"]


def test_skills_show_returns_body(session_id):
    _write_skill_file(
        session_id, "first-aaa.md",
        "# learned_skill: first\nhello\n",
    )
    payload = _run(show_skill_endpoint(
        name="first-aaa", session_id=session_id,
    ))
    assert "hello" in payload["body"]


def test_skills_show_prefix_match(session_id):
    _write_skill_file(
        session_id, "design-silicon-abc.md",
        "# learned_skill: design-silicon\nbody\n",
    )
    payload = _run(show_skill_endpoint(
        name="design-silicon", session_id=session_id,
    ))
    assert payload["name"] == "design-silicon-abc"


def test_skills_show_unknown_404(session_id):
    _write_skill_file(session_id, "x-aaa.md", "# x\n")
    with pytest.raises(Exception) as exc:
        _run(show_skill_endpoint(
            name="nothing-like-this", session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_skills_delete_removes_file(session_id):
    path = _write_skill_file(
        session_id, "doomed-zzz.md", "# doomed\n",
    )
    _run(delete_skill_endpoint(
        name="doomed-zzz", session_id=session_id,
    ))
    assert not path.exists()


def test_skills_export_default_target(session_id):
    _write_skill_file(
        session_id, "alpha-aaa.md", "# learned_skill: alpha\nbody\n",
    )
    payload = _run(export_skills_endpoint(
        body=SkillTransferRequest(),
        session_id=session_id,
    ))
    assert len(payload["copied"]) == 1
    # File landed in the default shared dir.
    shared = Path(os.environ["SIM2L_HOME"]) / "shared" / "skills"
    assert (shared / "alpha-aaa.md").exists()


def test_skills_export_to_custom_target(session_id, tmp_path):
    _write_skill_file(
        session_id, "beta-bbb.md", "# learned_skill: beta\nbody\n",
    )
    target = Path(os.environ["SIM2L_HOME"]) / "shared" / "skills" / "library"
    payload = _run(export_skills_endpoint(
        body=SkillTransferRequest(target=str(target)),
        session_id=session_id,
    ))
    assert (target / "beta-bbb.md").exists()
    assert payload["dst"] == str(target)


def test_skills_import_from_source(session_id, tmp_path):
    src = Path(os.environ["SIM2L_HOME"]) / "shared" / "skills" / "library"
    src.mkdir(parents=True)
    (src / "gamma-ccc.md").write_text(
        "# learned_skill: gamma\nshared\n", encoding="utf-8",
    )
    payload = _run(import_skills_endpoint(
        body=SkillTransferRequest(target=str(src)),
        session_id=session_id,
    ))
    assert "gamma-ccc" in payload["copied"]
    session_learned = (
        Path(os.environ["SIM2L_HOME"])
        / session_id / "skills" / "learned" / "gamma-ccc.md"
    )
    assert session_learned.exists()


def test_skills_import_conflict_without_force(session_id, tmp_path):
    _write_skill_file(
        session_id, "delta-ddd.md",
        "# learned_skill: delta\nsession version\n",
    )
    src = Path(os.environ["SIM2L_HOME"]) / "shared" / "skills" / "library"
    src.mkdir(parents=True)
    (src / "delta-ddd.md").write_text(
        "# learned_skill: delta\nshared version\n", encoding="utf-8",
    )
    payload = _run(import_skills_endpoint(
        body=SkillTransferRequest(target=str(src)),
        session_id=session_id,
    ))
    assert "delta-ddd" in payload["skipped_conflict"]
    # Session-local file unchanged.
    body = (
        Path(os.environ["SIM2L_HOME"])
        / session_id / "skills" / "learned" / "delta-ddd.md"
    ).read_text()
    assert "session version" in body


def test_skills_import_overwrites_with_force(session_id, tmp_path):
    _write_skill_file(
        session_id, "delta-ddd.md",
        "# learned_skill: delta\nold\n",
    )
    src = Path(os.environ["SIM2L_HOME"]) / "shared" / "skills" / "library"
    src.mkdir(parents=True)
    (src / "delta-ddd.md").write_text(
        "# learned_skill: delta\nnew\n", encoding="utf-8",
    )
    _run(import_skills_endpoint(
        body=SkillTransferRequest(target=str(src), force=True),
        session_id=session_id,
    ))
    body = (
        Path(os.environ["SIM2L_HOME"])
        / session_id / "skills" / "learned" / "delta-ddd.md"
    ).read_text()
    assert "new" in body


def test_skills_import_missing_source_404(session_id, tmp_path):
    with pytest.raises(Exception) as exc:
        _run(import_skills_endpoint(
            body=SkillTransferRequest(target="no-such-dir"),
            session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 404


def test_skills_transfer_rejects_target_outside_shared_root(session_id, tmp_path):
    _write_skill_file(
        session_id, "outside-aaa.md", "# learned_skill: outside\nbody\n",
    )
    with pytest.raises(Exception) as exc:
        _run(export_skills_endpoint(
            body=SkillTransferRequest(target=str(tmp_path / "outside")),
            session_id=session_id,
        ))
    assert getattr(exc.value, "status_code", None) == 400


# ── Server wiring ────────────────────────────────────────────────────


def test_server_includes_research_loop_router():
    """Sanity: the router is actually wired into create_app()."""
    from arc.api.server import create_app

    app = create_app()
    routes = {getattr(r, "path", None) for r in app.routes}
    assert "/strategies" in routes
    assert "/recipes" in routes
    assert "/clusters" in routes
    assert "/skills" in routes
