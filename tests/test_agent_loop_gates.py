"""Regression tests for final-answer quality gates in the agent loop."""
from unittest.mock import AsyncMock, patch

import pytest

from backend import db
from backend.agent_loop import run_task


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_final_answer_fails_when_branch_has_no_changes(monkeypatch):
    task = await db.create_task("No changes", "Finish without committing", "owner/repo")
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch", return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "final_answer", "tool": "", "input": {}, "reasoning": "done"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.github_compare_branch", return_value={
             "success": True,
             "data": {"has_changes": False, "files": [], "ahead_by": 0, "total_commits": 0},
         }), \
         patch("backend.agent_loop.github_create_pr") as create_pr, \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await run_task(task["id"])

    final = await db.get_task(task["id"])
    assert final["status"] == "failed"
    assert "No file changes" in final["error"]
    create_pr.assert_not_called()


@pytest.mark.asyncio
async def test_final_answer_fails_when_pr_creation_fails(monkeypatch):
    task = await db.create_task("PR failure", "Commit then finish", "owner/repo")
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch", return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "final_answer", "tool": "", "input": {}, "reasoning": "done"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.github_compare_branch", return_value={
             "success": True,
             "data": {
                 "has_changes": True,
                 "files": [{"filename": "backend/main.py"}],
                 "ahead_by": 1,
                 "total_commits": 1,
             },
         }), \
         patch("backend.agent_loop.run_tests", return_value={"success": True, "data": {"success": True, "results": []}}), \
         patch("backend.agent_loop.github_create_pr", return_value={"success": False, "error": "GithubException: 422 Unprocessable Entity"}), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await run_task(task["id"])

    final = await db.get_task(task["id"])
    assert final["status"] == "failed"
    assert "GitHub could not open the PR" in final["error"]
    assert not final.get("pr_url")


@pytest.mark.asyncio
async def test_final_answer_auto_runs_validation_and_opens_pr(monkeypatch):
    task = await db.create_task("Happy path", "Commit then finish", "owner/repo")
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch", return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "final_answer", "tool": "", "input": {}, "reasoning": "done"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.github_compare_branch", return_value={
             "success": True,
             "data": {
                 "has_changes": True,
                 "files": [{"filename": "frontend/index.html"}],
                 "ahead_by": 1,
                 "total_commits": 1,
             },
         }), \
         patch("backend.agent_loop.run_tests", return_value={"success": True, "data": {"success": True, "results": []}}) as tests, \
         patch("backend.agent_loop.github_create_pr", return_value={"success": True, "data": {"pr_url": "https://github.com/owner/repo/pull/1"}}), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await run_task(task["id"])

    final = await db.get_task(task["id"])
    assert final["status"] == "done"
    assert final["pr_url"] == "https://github.com/owner/repo/pull/1"
    tests.assert_called_once_with("frontend")

