"""Tests for API route validation and request plumbing."""

import pytest
from pydantic import ValidationError

from arc.api.routes import ResearchRequest, ReviewRequest, list_artifacts, list_results, run_review, start_research
from arc.schemas.execution import ExecutionResult
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


def test_research_request_rejects_non_positive_iterations():
    with pytest.raises(ValidationError):
        ResearchRequest(goal=ResearchGoal(goal="bad iterations"), iterations=0)


@pytest.mark.asyncio
async def test_run_review_requires_target_context():
    request = ReviewRequest(
        result=ExecutionResult(
            run_id="run-1",
            status="completed",
            outputs={"result": 1.0},
        )
    )
    with pytest.raises(Exception) as exc:
        await run_review(request)
    assert getattr(exc.value, "status_code", None) == 400
