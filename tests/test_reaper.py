"""
Tests for the zombie-task reaper in backend.main._reap_zombie_tasks.
DB isolation handled by autouse isolated_db fixture in conftest.py.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from backend import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _set_task_running_with_stale_times(task_id: str, minutes_ago: int = 15) -> None:
    """Force a task into running state with stale created_at and heartbeat_at."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    await db.update_task(task_id, status="running")
    async with db._get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET created_at=?, heartbeat_at=? WHERE id=?",
            (stale, stale, task_id),
        )


async def _set_task_running_no_heartbeat_old(task_id: str, minutes_ago: int = 15) -> None:
    """Running task with no heartbeat but old created_at (should be reaped)."""
    stale = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
    await db.update_task(task_id, status="running")
    async with db._get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET created_at=?, heartbeat_at=NULL WHERE id=?",
            (stale, task_id),
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_zombie_with_stale_heartbeat_is_reaped():
    """A running task with a stale heartbeat should be marked failed."""
    task = await db.create_task("Zombie", "test prompt")
    await _set_task_running_with_stale_times(task["id"], minutes_ago=15)

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "failed"
    assert "zombie" in updated["error"].lower()


@pytest.mark.asyncio
async def test_zombie_no_heartbeat_old_task_is_reaped():
    """A running task with no heartbeat and old created_at should be reaped."""
    task = await db.create_task("OldZombie", "test prompt")
    await _set_task_running_no_heartbeat_old(task["id"], minutes_ago=15)

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "failed"
    assert "zombie" in updated["error"].lower()


@pytest.mark.asyncio
async def test_fresh_running_task_not_reaped():
    """A task that just started (within 10 min) must NOT be reaped."""
    task = await db.create_task("FreshTask", "test prompt")
    await db.update_task(task["id"], status="running")
    # heartbeat is NULL but created_at is *now* (default), so under threshold

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "running"


@pytest.mark.asyncio
async def test_notifier_called_for_each_zombie():
    """notify_task_failure is called once per reaped zombie."""
    t1 = await db.create_task("Z1", "p")
    t2 = await db.create_task("Z2", "p")
    await _set_task_running_with_stale_times(t1["id"])
    await _set_task_running_with_stale_times(t2["id"])

    mock_notify = AsyncMock()
    with patch("backend.main.notify_task_failure", new=mock_notify):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    assert mock_notify.call_count == 2
    called_ids = {call.args[0] for call in mock_notify.call_args_list}
    assert t1["id"] in called_ids
    assert t2["id"] in called_ids


@pytest.mark.asyncio
async def test_completed_task_not_reaped():
    """A task with status='done' is never touched by the reaper."""
    task = await db.create_task("DoneTask", "test prompt")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    async with db._get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET status='done', created_at=?, heartbeat_at=? WHERE id=?",
            (stale, stale, task["id"]),
        )

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "done"


@pytest.mark.asyncio
async def test_touch_heartbeat_prevents_reaping():
    """After touch_heartbeat the task is no longer zombie-eligible."""
    task = await db.create_task("HeartbeatTask", "test prompt")
    stale = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    await db.update_task(task["id"], status="running")
    async with db._get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            (stale, task["id"]),
        )
    # Fresh heartbeat — within reaper threshold
    await db.touch_heartbeat(task["id"])

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    updated = await db.get_task(task["id"])
    assert updated["status"] == "running"
