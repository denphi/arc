"""GitHubSearcherAgent — read side of the GitHub backend (TODO item 15).

Lists artifacts published to the configured GitHub repo via the Contents
API and returns the ones matching the goal keywords as ``catalog_hits``.
Inactive (returns empty) when no GitHub config is present. Tests mock
``requests`` so no live API is hit.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from arc.core.strategies import resolve_role
from arc.schemas.research import ResearchGoal, SearchResult

pytestmark = pytest.mark.chat


def _agent():
    cls = resolve_role("searcher", overrides={"searcher": "github"})
    from arc.contracts.agent import AgentContext
    return cls(context=AgentContext(session_id="t", memory={}))


def test_github_searcher_inactive_without_config(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("ARC_GITHUB_REPO", raising=False)
    result = asyncio.run(_agent().search(ResearchGoal(goal="optimize bandgap")))
    assert isinstance(result, SearchResult)
    assert result.catalog_hits == []


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_github_searcher_lists_and_ranks_hits(monkeypatch):
    import requests
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ARC_GITHUB_REPO", "owner/repo")
    monkeypatch.delenv("ARC_GITHUB_BRANCH", raising=False)
    monkeypatch.delenv("ARC_GITHUB_PREFIX", raising=False)

    tree_payload = {"tree": [
        {"type": "blob", "path": "artifacts/bandgap_model/0.1.0/arc_record.json"},
        {"type": "blob", "path": "artifacts/bandgap_model/0.1.0/workflow.py"},
        {"type": "blob", "path": "artifacts/thermal_model/0.1.0/arc_record.json"},
    ]}
    records = {
        "artifacts/bandgap_model/0.1.0/arc_record.json": {
            "name": "bandgap_model", "description": "silicon bandgap",
            "metadata": {"sim2l_inputs": {"x": {}}, "sim2l_outputs": {"bandgap_ev": {}}},
        },
        "artifacts/thermal_model/0.1.0/arc_record.json": {
            "name": "thermal_model", "description": "heat transfer",
            "metadata": {},
        },
    }

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/git/trees/" in url:
            return _FakeResp(200, tree_payload)
        # contents/<path> raw fetch
        for path, rec in records.items():
            if url.endswith(path):
                return _FakeResp(200, text=json.dumps(rec))
        return _FakeResp(404)

    monkeypatch.setattr(requests, "get", fake_get)

    result = asyncio.run(_agent().search(ResearchGoal(goal="silicon bandgap study")))
    names = {h["name"] for h in result.catalog_hits}
    # "bandgap" keyword matches the bandgap_model; thermal_model does not.
    assert "bandgap_model" in names
    assert "thermal_model" not in names
    hit = next(h for h in result.catalog_hits if h["name"] == "bandgap_model")
    assert hit["output_schema"] == {"bandgap_ev": {}}
    assert hit["source"] == "github"


def test_github_searcher_tolerates_api_failure(monkeypatch):
    import requests
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ARC_GITHUB_REPO", "owner/repo")

    def boom(*a, **k):
        raise ConnectionError("down")
    monkeypatch.setattr(requests, "get", boom)

    result = asyncio.run(_agent().search(ResearchGoal(goal="anything")))
    assert result.catalog_hits == []
