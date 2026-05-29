"""Provider factory + the package-first provider model.

Core ships only the ``openwebui`` provider; anthropic/openai (and any
third-party provider) come from a package's ``provides.providers`` block
and are resolved through ``build_provider`` via the registry. These tests
pin that seam:
  * the factory resolves the core builtin without a registry,
  * an unknown/unset name degrades to ``None`` (stub mode),
  * a registry-registered provider class is resolved + constructed,
  * the bundled ``arc-providers`` package actually registers anthropic +
    openai through the real loader.
"""

from __future__ import annotations

import pytest

from arc.providers import build_provider, list_provider_names

pytestmark = pytest.mark.chat


def test_openwebui_is_the_core_builtin():
    p = build_provider("openwebui")
    assert type(p).__name__ == "OpenWebUIProvider"


def test_unset_name_is_stub_mode():
    assert build_provider("") is None
    assert build_provider(None) is None


def test_unknown_name_without_registry_is_none():
    # anthropic isn't a core builtin; with no registry it can't resolve.
    assert build_provider("anthropic") is None


def test_name_is_case_insensitive():
    assert type(build_provider("OpenWebUI")).__name__ == "OpenWebUIProvider"


class _StubRegistry:
    def __init__(self, providers):
        self._p = providers

    def get_provider(self, name):
        if name not in self._p:
            raise KeyError(name)
        return self._p[name]

    def list_providers(self):
        return list(self._p)


class _FakeProvider:
    name = "fake"

    def __init__(self, model=None, api_key=None):
        self.model = model
        self.api_key = api_key

    @classmethod
    def from_config(cls, *, token=None, model=None, base_url=None):
        return cls(model=model, api_key=token)


def test_registry_provider_is_resolved_via_from_config():
    reg = _StubRegistry({"fake": _FakeProvider})
    p = build_provider("fake", token="tok", model="m", registry=reg)
    assert isinstance(p, _FakeProvider)
    assert p.api_key == "tok" and p.model == "m"


def test_registry_provider_construct_failure_degrades_to_none():
    class _Broken:
        @classmethod
        def from_config(cls, **kw):
            raise RuntimeError("boom")
    reg = _StubRegistry({"broken": _Broken})
    assert build_provider("broken", registry=reg) is None


def test_list_provider_names_includes_builtin_and_registered():
    reg = _StubRegistry({"fake": _FakeProvider})
    names = list_provider_names(reg)
    assert "openwebui" in names      # core builtin
    assert "fake" in names           # registered


# ── the bundled arc-providers package registers through the real loader ────


def test_arc_providers_package_registers_anthropic_and_openai():
    from arc.orchestrator.workflow import _default_registry
    reg = _default_registry()
    assert "anthropic" in reg.list_providers()
    assert "openai" in reg.list_providers()
    # And the factory constructs them.
    assert type(build_provider("anthropic", token="x", registry=reg)).__name__ == "AnthropicProvider"
    assert type(build_provider("openai", registry=reg)).__name__ == "OpenAIProvider"


def test_core_providers_dir_has_only_openwebui():
    """The package-first principle, pinned: core keeps only openwebui."""
    from pathlib import Path
    import arc.providers as providers_pkg
    providers_dir = Path(providers_pkg.__file__).parent
    subpkgs = {p.name for p in providers_dir.iterdir() if p.is_dir() and not p.name.startswith("__")}
    assert subpkgs == {"openwebui"}, f"core providers should be only openwebui, got {subpkgs}"
