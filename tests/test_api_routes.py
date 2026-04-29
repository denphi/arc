"""Tests for API route validation and request plumbing."""

import pytest

from arc.api.routes import ResearchRequest, list_artifacts, list_results, start_research
from arc.schemas.research import ResearchGoal


@pytest.mark.asyncio
async def test_start_research_rejects_invalid_session_id():
    request = ResearchRequest(
        goal=ResearchGoal(goal="bad session"),
        session_id="../escape",
    )
    with pytest.raises(Exception) as exc:
        await start_research(request)
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_list_artifacts_rejects_invalid_session_id():
    with pytest.raises(Exception) as exc:
        await list_artifacts(session_id="../escape")
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_list_results_rejects_invalid_session_id():
    with pytest.raises(Exception) as exc:
        await list_results(session_id="../escape")
    assert getattr(exc.value, "status_code", None) == 400
