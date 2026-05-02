"""
Tests for PR-D3: restart recovery maturity.

Covers:
- checkpoint_phase set correctly during agent execution
- _recovery_note_for_interrupted_task uses checkpoint_phase for accurate notes
- _reconcile_interrupted_tasks is idempotent (no duplicate DB writes)
- Interrupted-flow reconciliation paths with all checkpoint variants
"""
import pytest
from unittest.mock import AsyncMock, patch

from backend import db
from backend.main import _recovery_note_for_interrupted_task, _reconcile_interrupted_tasks


# ---------------------------------------------------------------------------
# _recovery_note_for_interrupted_task — unit tests for all checkpoint states
# ---------------------------------------------------------------------------

def test_recovery_note_no_checkpoint_no_branch():
    task = {"branch": None, "pr_url": None, "checkpoint_phase": None}
    note = _recovery_note_for_interrupted_task(task)
    assert "before branch creation" in note
    assert "retry the task" in note


def test_recovery_note_checkpoint_post_branch_with_branch():
    task = {"branch": "task/my-feature", "pr_url": None, "checkpoint_phase": "post_branch"}
    note = _recovery_note_for_interrupted_task(task)
    assert "task/my-feature" in note
    assert "partial work" in note


def test_recovery_note_checkpoint_post_branch_without_branch_name():
    """checkpoint_phase=post_branch but branch column NULL (unlikely but safe)."""
    task = {"branch": None, "pr_url": None, "checkpoint_phase": "post_branch"}
    note = _recovery_note_for_interrupted_task(task)
    assert "partial work" in note
    assert "check GitHub" in note


def test_recovery_note_pr_url_with_checkpoint_post_pr():
    task = {
        "branch": "task/with-pr",
        "pr_url": "https://github.com/owner/repo/pull/42",
        "checkpoint_phase": "post_pr",
    }
    note = _recovery_note_for_interrupted_task(task)
    assert "PR already exists" in note
    assert "https://github.com/owner/repo/pull/42" in note


def test_recovery_note_checkpoint_post_pr_without_pr_url():
    """Crash after GitHub created the PR but before pr_url was written to DB."""
    task = {"branch": "task/crashed-pr", "pr_url": None, "checkpoint_phase": "post_pr"}
    note = _recovery_note_for_interrupted_task(task)
    assert "PR already exists" in note
    assert "check GitHub" in note


def test_recovery_note_pr_url_takes_priority_over_checkpoint_post_branch():
    """pr_url set → always shows PR note regardless of checkpoint_phase."""
    task = {
        "branch": "task/with-pr",
        "pr_url": "https://github.com/owner/repo/pull/7",
        "checkpoint_phase": "post_branch",
    }
    note = _recovery_note_for_interrupted_task(task)
    assert "PR already exists" in note
    assert "https://github.com/owner/repo/pull/7" in note


# ---------------------------------------------------------------------------
# _reconcile_interrupted_tasks — idempotency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_idempotent_second_call_skips_already_failed_task():
    """Calling reconcile twice must not re-write a task that is already failed."""
    task = await db.create_task("Interrupted", "prompt")
    await db.update_task(task["id"], status="running", branch="task/interrupted")

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        await _reconcile_interrupted_tasks()

    first = await db.get_task(task["id"])
    first_reconciled_at = first["reconciled_at"]
    assert first["status"] == "failed"

    # Second call — task is now failed, must not touch it
    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        await _reconcile_interrupted_tasks()

    second = await db.get_task(task["id"])
    # reconciled_at must not change on second call
    assert second["reconciled_at"] == first_reconciled_at
    assert second["status"] == "failed"


# ---------------------------------------------------------------------------
# checkpoint_phase recorded by agent_loop during execution
# ---------------------------------------------------------------------------

async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_agent_loop_sets_checkpoint_post_branch_after_branch_creation(monkeypatch):
    """checkpoint_phase is 'post_branch' after the branch is created."""
    from backend.agent_loop import run_task

    task = await db.create_task("Checkpoint test", "prompt", "owner/repo")
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/checkpoint-test"}}), \
         patch("backend.agent_loop.run_agent_turn", return_value=(
             "raw",
             {"action": "error", "tool": "", "input": {}, "reasoning": "deliberate stop"},
             {"input_tokens": 1, "output_tokens": 1},
         )), \
         patch("backend.agent_loop.record_and_check", new=AsyncMock()), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await run_task(task["id"])

    final = await db.get_task(task["id"])
    assert final["branch"] == "task/checkpoint-test"
    assert final["checkpoint_phase"] == "post_branch"


@pytest.mark.asyncio
async def test_agent_loop_sets_checkpoint_post_pr_before_changelog(monkeypatch):
    """checkpoint_phase='post_pr' and pr_url are persisted before CHANGELOG write."""
    from backend.agent_loop import run_task

    task = await db.create_task("PR checkpoint", "prompt", "owner/repo")
    monkeypatch.setattr("backend.agent_loop.settings.max_steps", 1)

    with patch("backend.agent_loop.asyncio.sleep", new=_no_sleep), \
         patch("backend.agent_loop.github_create_branch",
               return_value={"success": True, "data": {"branch": "task/pr-checkpoint"}}), \
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
         patch("backend.agent_loop.run_tests",
               return_value={"success": True, "data": {"success": True, "results": []}}), \
         patch("backend.agent_loop.github_create_pr",
               return_value={"success": True, "data": {"pr_url": "https://github.com/owner/repo/pull/99"}}), \
         patch("backend.agent_loop.notify_task_failure", new=AsyncMock()):
        await run_task(task["id"])

    final = await db.get_task(task["id"])
    assert final["status"] == "done"
    assert final["pr_url"] == "https://github.com/owner/repo/pull/99"
    assert final["checkpoint_phase"] == "post_pr"


# ---------------------------------------------------------------------------
# Reconciliation uses checkpoint_phase for accurate recovery notes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_uses_checkpoint_post_pr_for_recovery_note():
    """If checkpoint_phase=post_pr but pr_url is missing, note still mentions PR."""
    task = await db.create_task("Crashed post-PR", "prompt")
    await db.update_task(
        task["id"],
        status="running",
        branch="task/crashed-pr",
        checkpoint_phase="post_pr",
        # pr_url intentionally not set (crashed before DB write)
    )

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        await _reconcile_interrupted_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "failed"
    assert "PR already exists" in updated["recovery_note"]
    assert "check GitHub" in updated["recovery_note"]
    assert updated["reconciled_at"]


@pytest.mark.asyncio
async def test_reconcile_uses_checkpoint_post_branch_for_recovery_note():
    """checkpoint_phase=post_branch produces branch-review note even without pr_url."""
    task = await db.create_task("Crashed post-branch", "prompt")
    await db.update_task(
        task["id"],
        status="running",
        branch="task/my-branch",
        checkpoint_phase="post_branch",
    )

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        await _reconcile_interrupted_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "failed"
    assert "task/my-branch" in updated["recovery_note"]
    assert "partial work" in updated["recovery_note"]


@pytest.mark.asyncio
async def test_reconcile_no_checkpoint_no_branch_produces_early_restart_note():
    """No checkpoint, no branch → earliest-phase recovery message."""
    task = await db.create_task("Crashed early", "prompt")
    await db.update_task(task["id"], status="running")

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        await _reconcile_interrupted_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "failed"
    assert "before branch creation" in updated["recovery_note"]


# ---------------------------------------------------------------------------
# Reconciliation does not emit duplicate log entries when called twice
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_does_not_duplicate_log_entries():
    """Running reconcile twice must not append a second warning log to the task."""
    task = await db.create_task("Log dedup", "prompt")
    await db.update_task(task["id"], status="running", branch="task/log-dedup")

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        await _reconcile_interrupted_tasks()
        await _reconcile_interrupted_tasks()  # second call — task already failed

    logs = await db.get_logs(task["id"])
    warning_logs = [l for l in logs if l["level"] == "warning" and "interrupted" in l["message"]]
    assert len(warning_logs) == 1, "reconciler must log exactly once per interrupted task"
