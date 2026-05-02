"""
Tests for PR-B3 heartbeat resilience.

Validates:
1. _heartbeat_worker periodically updates heartbeat_at in the DB.
2. Worker cancels cleanly (no exception leak, CancelledError is swallowed).
3. Active heartbeat prevents the zombie reaper from false-positive reaping.
4. No heartbeat task remains running after run_task exits.
"""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from backend import db
from backend.agent_loop import _heartbeat_worker
from backend.config import settings


# ---------------------------------------------------------------------------
# 1. Worker updates DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_worker_updates_db():
    """_heartbeat_worker must update heartbeat_at after each interval fires."""
    task = await db.create_task("HBUpdate", "prompt")
    await db.update_task(task["id"], status="running")

    before = await db.get_task(task["id"])
    hb_task = asyncio.create_task(
        _heartbeat_worker(task["id"], interval=0.05),   # 50 ms for speed
    )
    await asyncio.sleep(0.25)   # allow ~4 intervals
    hb_task.cancel()
    await asyncio.gather(hb_task, return_exceptions=True)

    after = await db.get_task(task["id"])
    assert after["heartbeat_at"] is not None, "heartbeat_at must be set after worker fires"
    # The heartbeat should be very recent (within last 2 seconds)
    hb_str = after["heartbeat_at"]
    hb_dt = datetime.fromisoformat(hb_str)
    if hb_dt.tzinfo is None:
        hb_dt = hb_dt.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
    assert age < 2, f"heartbeat_at is {age:.2f}s old — expected < 2s"


# ---------------------------------------------------------------------------
# 2. Worker cancels cleanly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_heartbeat_worker_cancels_cleanly():
    """Cancelling _heartbeat_worker must not raise; CancelledError is swallowed."""
    task = await db.create_task("HBCancel", "prompt")

    hb_task = asyncio.create_task(
        _heartbeat_worker(task["id"], interval=60.0),   # Long sleep — cancel before it fires
    )
    await asyncio.sleep(0.01)
    hb_task.cancel()
    results = await asyncio.gather(hb_task, return_exceptions=True)

    assert hb_task.done(), "task must be finished after gather"
    # Worker swallows CancelledError → result is None (clean return), not an exception
    assert results == [None], f"expected [None] but got {results!r}"


# ---------------------------------------------------------------------------
# 3. Active heartbeat prevents zombie reaping
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_active_heartbeat_prevents_zombie_reap():
    """A task with an active heartbeat worker must not be reaped as a zombie
    even when its created_at is very old (simulating a long-running step)."""
    task = await db.create_task("LongStep", "prompt")

    # Make created_at very old so the reaper *would* catch it without heartbeats
    very_old = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    await db.update_task(task["id"], status="running")
    async with db._get_db() as conn:
        await conn.execute(
            "UPDATE tasks SET created_at=? WHERE id=?",
            (very_old, task["id"]),
        )

    # Start the heartbeat worker so heartbeat_at stays fresh
    hb_task = asyncio.create_task(
        _heartbeat_worker(task["id"], interval=0.05),
    )
    await asyncio.sleep(0.15)   # let it fire a few times

    with patch("backend.main.notify_task_failure", new=AsyncMock()):
        from backend.main import _reap_zombie_tasks
        await _reap_zombie_tasks()

    hb_task.cancel()
    await asyncio.gather(hb_task, return_exceptions=True)

    updated = await db.get_task(task["id"])
    assert updated["status"] == "running", (
        "Task with active heartbeat must NOT be reaped as zombie"
    )


# ---------------------------------------------------------------------------
# 4. No heartbeat task leak after run_task exits
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_heartbeat_task_leak_after_run_task_early_exit():
    """run_task always cancels and awaits the heartbeat task before returning,
    so no background heartbeat-* task survives after the function exits."""
    task = await db.create_task("Leak", "prompt")

    created_tasks: list[asyncio.Task] = []
    _real_create_task = asyncio.get_event_loop().create_task

    def _spy_create_task(coro, *, name=None, **kw):
        t = asyncio.ensure_future(coro)
        if name and name.startswith("heartbeat-"):
            created_tasks.append(t)
        return t

    with patch("asyncio.create_task", side_effect=_spy_create_task):
        # Simulate a very early exit: task not found in DB → run_task returns quickly.
        # We patch db.get_task to return None right after status is set.
        original_get_task = db.get_task
        call_count = 0

        async def _patched_get_task(tid):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None   # Cause early return
            return await original_get_task(tid)

        with patch("backend.agent_loop.db.get_task", side_effect=_patched_get_task), \
             patch("backend.agent_loop.db.update_task", new=AsyncMock()), \
             patch("backend.agent_loop.db.touch_heartbeat", new=AsyncMock()), \
             patch("backend.agent_loop.emit_log", new=AsyncMock()), \
             patch("backend.agent_loop.destroy_bus", new=MagicMock()), \
             patch("backend.agent_loop.asyncio.sleep", new=AsyncMock()):
            from backend.agent_loop import run_task
            await run_task(task["id"])

    # All heartbeat tasks created during run_task must be done by now
    for hb in created_tasks:
        assert hb.done(), "Heartbeat task must be done after run_task exits (no leak)"
