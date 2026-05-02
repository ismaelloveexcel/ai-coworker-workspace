"""
Tests for PR-B4: cancellation side-effect safety.

Verify that once cancellation is acknowledged via request_cancel():
1. No new write operation is started.
2. In-flight completion after cancel is recorded explicitly.
3. DB/state reflects the final truth without ambiguity.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import db
from backend.agent_loop import _cancel_requested, request_cancel, run_task


async def _no_sleep(_seconds):
    return None


async def _run_and_absorb_cancel(task_id: str) -> None:
    """Run a task, absorbing the expected CancelledError that run_task re-raises."""
    try:
        await run_task(task_id)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Checkpoint: branch creation blocked when cancelled before it starts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_branch_creation_blocked_by_cancel_checkpoint():
    """Cancellation requested before branch creation prevents the branch write."""
    task = await db.create_task("Cancel before branch", "prompt", "owner/repo")
    task_id = task["id"]

    # Pre-populate the cancel set — mimics the API calling request_cancel()
    # before run_task's first checkpoint is reached.
    request_cancel(task_id)

    branch_mock = MagicMock(return_value={"success": True, "data": {"branch": "task/nope"}})

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch", new=branch_mock), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    # Branch creation must not have been called
    branch_mock.assert_not_called()

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    # _cancel_requested cleaned up in finally
    assert task_id not in _cancel_requested


# ---------------------------------------------------------------------------
# Checkpoint: tool-call write blocked when cancelled between steps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_write_tool_call_blocked_by_cancel_checkpoint(monkeypatch):
    """Cancellation requested before a tool call prevents the write tool from running."""
    task = await db.create_task("Cancel before tool", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    execute_mock = MagicMock(return_value={"success": True, "data": {}})

    def set_cancel_and_return(*args, **kwargs):
        # Called instead of run_agent_turn — signals cancel first
        request_cancel(task_id)
        return (
            "raw",
            {"action": "tool_call", "tool": "github_commit_files",
             "input": {"files": []}, "reasoning": "write"},
            {"input_tokens": 1, "output_tokens": 1},
        )

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", side_effect=set_cancel_and_return), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.execute_tool", new=execute_mock), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    # execute_tool must NOT have been called (blocked by checkpoint)
    execute_mock.assert_not_called()

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert task_id not in _cancel_requested


# ---------------------------------------------------------------------------
# Checkpoint: PR creation blocked when cancelled after branch + commits done
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pr_creation_blocked_by_cancel_checkpoint(monkeypatch):
    """Cancellation requested before final_answer PR creation prevents the write."""
    task = await db.create_task("Cancel before PR", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    pr_mock = MagicMock(return_value={"success": True, "data": {"pr_url": "https://gh/1"}})

    def _agent_turn_with_cancel(*args, **kwargs):
        # Cancel just before final_answer triggers PR creation
        request_cancel(task_id)
        return (
            "raw",
            {"action": "final_answer", "tool": "", "input": {}, "reasoning": "done"},
            {"input_tokens": 1, "output_tokens": 1},
        )

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", side_effect=_agent_turn_with_cancel), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.github_compare_branch", return_value={
             "success": True,
             "data": {"has_changes": True, "files": [{"filename": "x.py"}],
                      "ahead_by": 1, "total_commits": 1},
         }), \
         patch("backend.agent_loop.run_tests",
               return_value={"success": True, "data": {"success": True, "results": []}}), \
         patch("backend.agent_loop.github_create_pr", new=pr_mock), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    pr_mock.assert_not_called()

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert task_id not in _cancel_requested


# ---------------------------------------------------------------------------
# Checkpoint: changelog commit blocked when cancelled after PR created
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_changelog_commit_blocked_by_cancel_checkpoint(monkeypatch):
    """Cancellation requested between PR creation and changelog commit stops the write."""
    task = await db.create_task("Cancel before changelog", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    def _pr_create_and_cancel(branch, title, body, repo):
        # PR creation completes, then we register cancel so changelog is blocked
        request_cancel(task_id)
        return {"success": True, "data": {"pr_url": "https://gh/pr/99"}}

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "final_answer", "tool": "", "input": {}, "reasoning": "done"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.github_compare_branch", return_value={
             "success": True,
             "data": {"has_changes": True, "files": [{"filename": "x.py"}],
                      "ahead_by": 1, "total_commits": 1},
         }), \
         patch("backend.agent_loop.run_tests",
               return_value={"success": True, "data": {"success": True, "results": []}}), \
         patch("backend.agent_loop.github_create_pr", side_effect=_pr_create_and_cancel), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    # Task must be cancelled, not "done", because changelog commit was blocked
    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert task_id not in _cancel_requested


# ---------------------------------------------------------------------------
# Checkpoint: notification issue creation skipped when cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notification_skipped_when_cancelled(monkeypatch):
    """notify_task_failure is not called when cancellation is acknowledged."""
    task = await db.create_task("Cancel + fail combo", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    notify_mock = AsyncMock()
    # Cancel BEFORE the run — checkpoint blocks branch creation AND notification
    request_cancel(task_id)

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.notify_task_failure", new=notify_mock):
        await _run_and_absorb_cancel(task_id)

    notify_mock.assert_not_called()

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Checkpoint: max-steps PR creation blocked when cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_max_steps_pr_creation_blocked_by_cancel_checkpoint(monkeypatch):
    """Cancellation between compare and max-steps PR creation prevents the write."""
    task = await db.create_task("Cancel before max-steps PR", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    pr_mock = MagicMock(return_value={"success": True, "data": {"pr_url": "https://gh/draft/1"}})

    def _compare_and_cancel(branch, base, repo):
        # Compare returns changes; simultaneously request cancel
        request_cancel(task_id)
        return {
            "success": True,
            "data": {"has_changes": True, "files": [{"filename": "x.py"}],
                     "ahead_by": 1, "total_commits": 1},
        }

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "tool_call", "tool": "some_tool", "input": {}, "reasoning": ""},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.execute_tool",
               return_value={"success": True, "data": {}}), \
         patch("backend.agent_loop.github_compare_branch", side_effect=_compare_and_cancel), \
         patch("backend.agent_loop.github_create_pr", new=pr_mock), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    pr_mock.assert_not_called()

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert task_id not in _cancel_requested


# ---------------------------------------------------------------------------
# In-flight: branch creation completes after cancel — state recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_flight_branch_creation_records_state():
    """When CancelledError arrives during branch creation, state is recorded explicitly."""
    task = await db.create_task("In-flight branch", "prompt", "owner/repo")
    task_id = task["id"]

    # Simulate asyncio delivering CancelledError while branch creation is in-flight.
    # This is what happens when task.cancel() fires while awaiting run_in_executor.
    def _branch_raises_cancelled(tid, repo):
        raise asyncio.CancelledError("in-flight branch creation")

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               side_effect=_branch_raises_cancelled), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert final.get("error") is not None
    assert "branch" in (final.get("error") or "").lower()


# ---------------------------------------------------------------------------
# In-flight: PR creation completes after cancel — state recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_flight_pr_creation_records_state(monkeypatch):
    """When CancelledError arrives during PR creation, state is recorded explicitly."""
    task = await db.create_task("In-flight PR", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    def _pr_raises_cancelled(branch, title, body, repo):
        raise asyncio.CancelledError("in-flight pr creation")

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "final_answer", "tool": "", "input": {}, "reasoning": "done"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.github_compare_branch", return_value={
             "success": True,
             "data": {"has_changes": True, "files": [{"filename": "x.py"}],
                      "ahead_by": 1, "total_commits": 1},
         }), \
         patch("backend.agent_loop.run_tests",
               return_value={"success": True, "data": {"success": True, "results": []}}), \
         patch("backend.agent_loop.github_create_pr", side_effect=_pr_raises_cancelled), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert final.get("error") is not None
    assert "PR" in (final.get("error") or "")


# ---------------------------------------------------------------------------
# In-flight: write tool call completes after cancel — state recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_flight_write_tool_records_state(monkeypatch):
    """When a write tool (github_commit_files) is in-flight and cancelled, state is recorded."""
    task = await db.create_task("In-flight write tool", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    def _execute_raises_cancelled(tool_name, tool_input, t_id):
        raise asyncio.CancelledError("in-flight commit")

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "tool_call", "tool": "github_commit_files",
              "input": {"files": []}, "reasoning": "commit"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.execute_tool", side_effect=_execute_raises_cancelled), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    assert final.get("error") is not None
    assert "github_commit_files" in (final.get("error") or "")


# ---------------------------------------------------------------------------
# In-flight: non-write tool cancelled — no explicit error recorded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_in_flight_non_write_tool_no_extra_error(monkeypatch):
    """For non-write tool in-flight cancellations, we still cancel but no write-specific error."""
    task = await db.create_task("In-flight read tool", "prompt", "owner/repo")
    task_id = task["id"]
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    def _execute_raises_cancelled(tool_name, tool_input, t_id):
        raise asyncio.CancelledError("in-flight read")

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "tool_call", "tool": "github_read_file",
              "input": {}, "reasoning": "read"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.execute_tool", side_effect=_execute_raises_cancelled), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    final = await db.get_task(task_id)
    assert final["status"] == "cancelled"
    # For non-write tools there is no in-flight write error; error field is None
    assert not final.get("error")


# ---------------------------------------------------------------------------
# request_cancel() / _cancel_requested cleanup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_requested_set_cleaned_up_after_run():
    """_cancel_requested is cleaned up in finally even when cancellation fires."""
    task = await db.create_task("Cleanup test", "prompt", "owner/repo")
    task_id = task["id"]

    request_cancel(task_id)
    assert task_id in _cancel_requested

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/test"}}), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await _run_and_absorb_cancel(task_id)

    assert task_id not in _cancel_requested

