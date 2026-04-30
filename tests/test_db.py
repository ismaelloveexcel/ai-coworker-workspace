"""
Tests for backend.db — migrations, column allowlist, health_check.
Isolation handled by the autouse isolated_db fixture in conftest.py.
"""
import pytest
from backend import db


@pytest.mark.asyncio
async def test_init_and_create_task():
    task = await db.create_task("Test", "Do something")
    assert task["status"] == "pending"
    assert task["title"] == "Test"


@pytest.mark.asyncio
async def test_get_task_returns_none_for_missing():
    result = await db.get_task("nonexistent-id")
    assert result is None


@pytest.mark.asyncio
async def test_update_task_valid_column():
    task = await db.create_task("T", "P")
    await db.update_task(task["id"], status="running")
    updated = await db.get_task(task["id"])
    assert updated["status"] == "running"


@pytest.mark.asyncio
async def test_update_task_rejects_invalid_column():
    task = await db.create_task("T", "P")
    with pytest.raises(ValueError, match="disallowed"):
        await db.update_task(task["id"], malicious_col="DROP TABLE tasks")


@pytest.mark.asyncio
async def test_list_tasks_pagination():
    for i in range(5):
        await db.create_task(f"Task {i}", "prompt")
    page1 = await db.list_tasks(limit=3, offset=0)
    page2 = await db.list_tasks(limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) >= 2
    ids1 = {t["id"] for t in page1}
    ids2 = {t["id"] for t in page2}
    assert ids1.isdisjoint(ids2)


@pytest.mark.asyncio
async def test_health_check_returns_true():
    ok = await db.health_check()
    assert ok is True


@pytest.mark.asyncio
async def test_create_and_get_step():
    task = await db.create_task("T", "P")
    await db.create_step(task["id"], 1, tool_name="github_read_file")
    steps = await db.get_steps(task["id"])
    assert len(steps) == 1
    assert steps[0]["tool_name"] == "github_read_file"
    assert steps[0]["status"] == "running"


@pytest.mark.asyncio
async def test_migration_idempotent():
    # Running init_db twice should not raise
    await db.init_db()
    await db.init_db()


@pytest.mark.asyncio
async def test_busy_timeout_set_after_init_db():
    """B5: every connection must have busy_timeout=5000 after init_db()."""
    await db.init_db()
    async with db._get_db() as conn:
        row = await (await conn.execute("PRAGMA busy_timeout")).fetchone()
    assert row[0] == 5000
