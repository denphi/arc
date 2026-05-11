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
    ]:
        monkeypatch.delenv(name, raising=False)
