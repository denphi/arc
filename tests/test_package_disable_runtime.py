"""`/package disable` is a real runtime filter (design/todo.md item 4).

Previously the toggle only relabelled session state; a disabled package's
strategies stayed selectable. Now ``resolve_role`` refuses a strategy owned
by a disabled package and falls back to an enabled one.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arc.core.strategies import resolve_role
from arc.packages import resolve_role as pkg_resolve_role

pytestmark = pytest.mark.chat


def test_disabled_package_strategy_falls_back_to_default():
    # mars_planner lives in arc-mars. Selecting it while arc-mars is
    # disabled must fall back to the default planner (arc-sim2l).
    cls = resolve_role(
        "planner",
        overrides={"planner": "mars_planner"},
        disabled_packages={"arc-mars"},
    )
    assert cls.__name__ == "PlannerAgent"  # the arc-sim2l default


def test_disabled_package_strategy_selectable_when_enabled():
    cls = resolve_role("planner", overrides={"planner": "mars_planner"})
    assert cls.__name__ == "MARSPlannerAgent"


def test_composite_drops_disabled_components():
    # A searcher stack that names a materials_project component (arc-materials)
    # should drop it when arc-materials is disabled, keeping the rest.
    cls = resolve_role(
        "searcher",
        overrides={"searcher": "default embeddings materials_project"},
        disabled_packages={"arc-materials"},
    )
    # Two survivors → still a composite; its declared component list excludes
    # the materials_project source.
    assert getattr(cls, "strategy_names", ()) == ("default", "embeddings")


def test_composite_collapses_to_single_when_all_but_one_disabled():
    cls = resolve_role(
        "searcher",
        overrides={"searcher": "default materials_project"},
        disabled_packages={"arc-materials"},
    )
    # Only the arc-sim2l "default" survives → resolves to the single searcher.
    assert cls.__name__ == "KeywordSearcherAgent"


def test_pkg_wrapper_reads_session_disabled_set():
    """The chat/UI-facing wrapper reads memory['packages']['disabled']."""
    workflow = SimpleNamespace(_context=SimpleNamespace(memory={
        "strategy_overrides": {"planner": "mars_planner"},
        "packages": {"disabled": ["arc-mars"], "enabled": []},
    }))
    cls = pkg_resolve_role("planner", workflow)
    assert cls.__name__ == "PlannerAgent"  # fell back, arc-mars disabled


# ── Finding 1: disable filters ALL package-owned component types ─────────


def _registry_with_pkg_components():
    from arc.core.registry import ComponentRegistry
    reg = ComponentRegistry()
    reg.register_skill("pkg_skill", object(), package_name="arc-demo")
    reg.register_provider("pkg_provider", object(), package_name="arc-demo")
    reg.register_adapter("pkg_adapter", object(), package_name="arc-demo")
    # A core (no package) component must never be filtered.
    reg.register_skill("core_skill", object())
    return reg


def test_disabled_package_skill_unavailable():
    reg = _registry_with_pkg_components()
    disabled = {"arc-demo"}
    # Enabled lookup works; disabled raises.
    assert reg.get_skill("pkg_skill") is not None
    with pytest.raises(KeyError):
        reg.get_skill("pkg_skill", disabled_packages=disabled)
    # Core skill is unaffected.
    assert reg.get_skill("core_skill", disabled_packages=disabled) is not None
    # list_skills drops the disabled one.
    assert "pkg_skill" not in reg.list_skills(disabled_packages=disabled)
    assert "core_skill" in reg.list_skills(disabled_packages=disabled)


def test_disabled_package_adapter_unavailable():
    reg = _registry_with_pkg_components()
    with pytest.raises(KeyError):
        reg.get_adapter("pkg_adapter", disabled_packages={"arc-demo"})
    assert reg.get_adapter("pkg_adapter") is not None


def test_disabled_package_provider_not_built():
    from arc.providers import build_provider

    class _FakeProvider:
        def __init__(self, **kw):
            pass

    reg = _registry_with_pkg_components()
    reg.register_provider("fake", _FakeProvider, package_name="arc-demo")

    # Enabled → built; disabled → None (stub mode), never instantiated.
    assert build_provider("fake", registry=reg) is not None
    assert build_provider("fake", registry=reg, disabled_packages={"arc-demo"}) is None


def test_filter_disabled_only_touches_named_packages():
    reg = _registry_with_pkg_components()
    reg.register_skill("other_pkg_skill", object(), package_name="arc-other")
    kept = reg.list_skills(disabled_packages={"arc-demo"})
    assert "other_pkg_skill" in kept   # different package, still available
    assert "pkg_skill" not in kept


# ── Finding P2-1: direct package-agents honour disable ───────────────────


def test_disabled_package_agent_unavailable_by_bare_name():
    from arc.core.registry import ComponentRegistry

    reg = ComponentRegistry()

    class _Sup:
        pass

    reg.register_agent("coscientist_supervisor", _Sup, package_name="arc-coscientist")
    # Enabled lookup works.
    assert reg.get_agent("coscientist_supervisor") is _Sup
    # A direct (non-role) lookup honours /package disable.
    with pytest.raises(KeyError):
        reg.get_agent("coscientist_supervisor", disabled_packages={"arc-coscientist"})
    # The explicit package-selector form too.
    assert reg.get_agent("coscientist_supervisor", "arc-coscientist") is _Sup
    with pytest.raises(KeyError):
        reg.get_agent(
            "coscientist_supervisor", "arc-coscientist",
            disabled_packages={"arc-coscientist"},
        )


def test_workflow_direct_agent_step_honours_disable():
    """A workflow step naming a package agent directly (not a role) must not
    run it when its package is disabled (review finding P2-1)."""
    from arc.orchestrator.workflow import ResearchWorkflow

    workflow = ResearchWorkflow()

    class _PkgAgent:
        def __init__(self, context=None):
            self.context = context

    # A name no bundled package claims, so this exercises disable alone — a
    # contested name would instead exercise the fall-back-to-another-provider
    # path (see test_kernel_loader.py).
    workflow.registry.register_agent(
        "demo_only_decomposer", _PkgAgent, package_name="arc-demo",
    )
    # Enabled → resolves.
    assert workflow._resolve_agent_class("demo_only_decomposer") is _PkgAgent
    # Disable arc-demo for the session → direct lookup raises.
    workflow._context.memory["packages"] = {"disabled": ["arc-demo"]}
    with pytest.raises(KeyError):
        workflow._resolve_agent_class("demo_only_decomposer")


# ── Finding P2-2: extension-created components keep package ownership ─────


def test_extension_scoped_registry_attributes_package():
    from arc.core.registry import ComponentRegistry

    reg = ComponentRegistry()
    scoped = reg.scoped("arc-openapi")
    # Extension code calls the plain register API…
    scoped.register_skill("openapi::svc::op", object())
    scoped.register_evaluator("openapi_eval", object())   # source-only path
    # …yet the components are attributed to the extension's package.
    assert reg.component_source("skill", "openapi::svc::op") == "arc-openapi"
    assert reg.component_source("evaluator", "openapi_eval") == "arc-openapi"
    # So disabling the package filters them.
    with pytest.raises(KeyError):
        reg.get_skill("openapi::svc::op", disabled_packages={"arc-openapi"})


def test_scoped_registry_noop_without_package():
    from arc.core.registry import ComponentRegistry

    reg = ComponentRegistry()
    reg.scoped(None).register_skill("plain", object())
    assert reg.component_source("skill", "plain") is None
    assert reg.get_skill("plain", disabled_packages={"anything"}) is not None


# ── Finding P2: disabling a provider/adapter package rebuilds the live
#    workflow.provider / workflow.adapter instances ───────────────────────


class _FakeProvider:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


def test_disabling_provider_package_drops_live_provider():
    """A provider package disabled mid-session must invalidate the already-
    built workflow.provider + context.memory['provider'] (review finding P2)."""
    from arc.orchestrator.workflow import ResearchWorkflow

    workflow = ResearchWorkflow()
    workflow.registry.register_provider("fakeprov", _FakeProvider, package_name="arc-fake")
    workflow._provider_build_args["provider_name"] = "fakeprov"

    # Build it while enabled.
    workflow.refresh_disabled_packages()
    assert workflow.provider is not None
    assert workflow._context.memory["provider"] is workflow.provider

    # Disable the package mid-session → provider drops to stub mode (None),
    # and the context reference is kept in sync.
    workflow._context.memory["packages"] = {"disabled": ["arc-fake"]}
    workflow.refresh_disabled_packages()
    assert workflow.provider is None
    assert workflow._context.memory["provider"] is None


def test_disabling_adapter_package_falls_back_to_local():
    """A runtime-adapter package disabled mid-session must rebuild
    workflow.adapter to the local adapter (review finding P2 + P3)."""
    import os

    from arc.orchestrator.workflow import ResearchWorkflow
    from arc.runtime.local import LocalRuntimeAdapter

    class _FakeAdapter(LocalRuntimeAdapter):
        pass

    workflow = ResearchWorkflow()
    workflow.registry.register_adapter("fakeadapter", _FakeAdapter, package_name="arc-fake")

    # Point ARC_RUNTIME_ADAPTER at the package adapter and rebuild.
    prev = os.environ.get("ARC_RUNTIME_ADAPTER")
    os.environ["ARC_RUNTIME_ADAPTER"] = "fakeadapter"
    try:
        workflow.refresh_disabled_packages()
        assert isinstance(workflow.adapter, _FakeAdapter)
        assert workflow._context.memory["adapter"] is workflow.adapter

        # Disable the package → adapter falls back to the built-in local one.
        workflow._context.memory["packages"] = {"disabled": ["arc-fake"]}
        workflow.refresh_disabled_packages()
        assert type(workflow.adapter) is LocalRuntimeAdapter
        assert workflow._context.memory["adapter"] is workflow.adapter
    finally:
        if prev is None:
            os.environ.pop("ARC_RUNTIME_ADAPTER", None)
        else:
            os.environ["ARC_RUNTIME_ADAPTER"] = prev


def test_build_adapter_skips_disabled_package_adapter():
    """_build_adapter honours the disabled set at construction-time lookup
    (review finding P3)."""
    import os

    from arc.core.registry import ComponentRegistry
    from arc.orchestrator.workflow import _build_adapter
    from arc.runtime.local import LocalRuntimeAdapter

    class _FakeAdapter(LocalRuntimeAdapter):
        pass

    reg = ComponentRegistry()
    reg.register_adapter("fakeadapter", _FakeAdapter, package_name="arc-fake")

    prev = os.environ.get("ARC_RUNTIME_ADAPTER")
    os.environ["ARC_RUNTIME_ADAPTER"] = "fakeadapter"
    try:
        # Enabled → the package adapter.
        assert isinstance(_build_adapter(registry=reg), _FakeAdapter)
        # Disabled → falls back to local, never the package adapter.
        adapter = _build_adapter(registry=reg, disabled_packages={"arc-fake"})
        assert type(adapter) is LocalRuntimeAdapter
    finally:
        if prev is None:
            os.environ.pop("ARC_RUNTIME_ADAPTER", None)
        else:
            os.environ["ARC_RUNTIME_ADAPTER"] = prev


def test_chat_loop_refreshes_after_restoring_disabled_packages(monkeypatch):
    """Resuming chat restores package state after workflow construction; the
    loop must refresh provider/adapter references before accepting input."""
    import asyncio
    from types import SimpleNamespace

    from arc.chat import loop as chat_loop_mod

    calls = []
    workflow = SimpleNamespace(
        session_id="resume-session",
        _context=SimpleNamespace(memory={}),
        refresh_disabled_packages=lambda: calls.append(dict(workflow._context.memory)),
    )

    def _restore(workflow_arg):
        workflow_arg._context.memory["packages"] = {"disabled": ["arc-fake"]}
        return None

    async def _raise_eof(*args, **kwargs):
        raise EOFError

    monkeypatch.setattr(chat_loop_mod, "_restore_session", _restore)
    monkeypatch.setattr(chat_loop_mod, "_materialise_pending_sink", lambda workflow: None)
    monkeypatch.setattr(chat_loop_mod, "_check_sim2l_services", lambda: {})
    monkeypatch.setattr(chat_loop_mod, "print_banner", lambda *args, **kwargs: None)
    monkeypatch.setattr(chat_loop_mod, "_selected_coder", lambda workflow: "default")
    monkeypatch.setattr(chat_loop_mod, "chat_input_async", _raise_eof)

    import arc.services as services
    monkeypatch.setattr(services, "sim2l_available", lambda: False)

    asyncio.run(chat_loop_mod.chat_loop(workflow, None, None, None, max_iterations=1))

    assert calls
    assert calls[0]["packages"] == {"disabled": ["arc-fake"]}


def test_api_workflow_hydrates_packages_and_refreshes(monkeypatch):
    """API-created workflows hydrate persisted package state and refresh the
    already-built provider/adapter references."""
    from types import SimpleNamespace

    from arc.api import routes

    refresh_calls = []

    class _FakeWorkflow:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.session_id = kwargs["session_id"]
            self._context = SimpleNamespace(memory={})

        def refresh_disabled_packages(self):
            refresh_calls.append(dict(self._context.memory))

    monkeypatch.setattr(routes, "ResearchWorkflow", _FakeWorkflow)

    import arc.api.session_state as session_state
    import arc.session as session_mod
    monkeypatch.setattr(session_state, "load_state", lambda session_id: {
        "strategy_overrides": {"planner": "mars_planner"},
    })
    monkeypatch.setattr(session_mod, "load_session_meta", lambda session_id: {
        "packages": {"disabled": ["arc-mars"]},
    })

    workflow = routes._workflow(routes.LLMConfig(), session_id="api-session")

    assert workflow._context.memory["strategy_overrides"] == {"planner": "mars_planner"}
    assert workflow._context.memory["packages"] == {"disabled": ["arc-mars"]}
    assert refresh_calls
    assert refresh_calls[0]["packages"] == {"disabled": ["arc-mars"]}
