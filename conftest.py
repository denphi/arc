"""pytest configuration — adds the project root to sys.path."""

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Keep Matplotlib's test-time cache inside the workspace. This avoids the
# cma/matplotlib warning when the user's home cache is not writable, without
# requiring every test command to prefix MPLCONFIGDIR=...
_MPL_CACHE = ROOT.parent / ".venv" / "matplotlib-cache"
_MPL_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CACHE))


@pytest.fixture(autouse=True)
def isolated_sim2l_home(tmp_path, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path / "sim2l-home"))
    monkeypatch.setenv("ARC_STORAGE_MODE", "local")
    for name in [
        "ARC_PROVIDER",
        "ARC_MODEL",
        "OPENWEBUI_KEY",
        "OPENWEBUI_URL",
        "OPENWEBUI_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        # Security knobs read by arc.api.security — keep tests deterministic.
        "ARC_API_TOKEN",
        "ARC_PROVIDER_ALLOWLIST",
        "ARC_ALLOW_PRIVATE_PROVIDER_HOSTS",
    ]:
        monkeypatch.delenv(name, raising=False)

    # A checked-in ``arc/.env`` (with real provider creds) must never leak
    # into the suite: ``load_env()`` fills unset keys from that file, so the
    # delenv() above is undone the first time a test constructs a workflow
    # and the orchestrator calls load_env(). Mark the loader as already-run
    # so it stays a no-op; tests that genuinely want a provider set the env
    # vars explicitly. This keeps stub-mode tests deterministic and fast
    # regardless of import/test ordering (previously the first test to
    # trigger load_env got a live OpenWebUI provider and a ~30s network
    # timeout per run).
    try:
        import arc.core.env as _arc_env

        monkeypatch.setattr(_arc_env, "_loaded", True, raising=False)
    except ImportError:
        pass

    # arc.api.security caches its config — drop the cache so per-test
    # monkeypatching of the above envs takes effect.
    try:
        from arc.api.security import load_security_config

        load_security_config.cache_clear()
    except ImportError:
        # arc.api.security isn't imported on every test path.
        pass

    # Review item #T17: the shared native-JSON capability cache also needs
    # to be reset between tests so a probe in one test doesn't bleed into
    # the next.
    try:
        from arc.providers.utils import reset_native_json_support

        reset_native_json_support()
    except ImportError:
        pass

    # Chat plan-mode and event sink are global singletons; reset between
    # tests so CLI tests that set --plan don't leak into the next test.
    try:
        from arc.chat.plan_mode import set_plan_mode
        set_plan_mode(False)
    except ImportError:
        pass
    try:
        from arc.chat.events import set_sink, set_sink_config
        set_sink(None)
        set_sink_config(None)
    except ImportError:
        pass

    # The strategy catalogue is a module global; tests that load scaffolded
    # packages (``arc package init``) call register_strategy() and would
    # otherwise leak entries like ``my_lab_ideator`` into later tests that
    # pin the catalogue. Restore the bundled baseline before each test.
    try:
        from arc.core.strategies import reset_strategy_catalogue
        reset_strategy_catalogue()
    except ImportError:
        pass
