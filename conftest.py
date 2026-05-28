"""pytest configuration — adds the project root to sys.path."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
