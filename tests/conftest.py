"""Suite-wide isolation.

Every test gets a private ``SIM2L_HOME`` so session/artifact/result writes
never touch the developer's real ``~/.sim2l``. Tests that need a specific
root still override it with ``monkeypatch.setenv``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_sim2l_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("SIM2L_HOME", str(tmp_path_factory.mktemp("sim2l_home")))
