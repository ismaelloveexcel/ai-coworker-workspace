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
async def test_task_recovery_columns_exist_and_are_updatable():
    task = await db.create_task("Recoverable", "Prompt")
    await db.update_task(
        task["id"],
        branch="task/abc",
        current_step=3,
        last_action="tool_call",
        last_tool="github_commit_files",
        recovery_note="Review branch task/abc",
        reconciled_at="2026-05-02T00:00:00+00:00",
    )

    updated = await db.get_task(task["id"])
    assert updated["branch"] == "task/abc"
    assert updated["current_step"] == 3
    assert updated["last_action"] == "tool_call"
    assert updated["last_tool"] == "github_commit_files"
    assert "Review branch" in updated["recovery_note"]
    assert updated["reconciled_at"] == "2026-05-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_get_running_tasks_returns_only_running_tasks():
    running = await db.create_task("Running", "Prompt")
    done = await db.create_task("Done", "Prompt")
    await db.update_task(running["id"], status="running")
    await db.update_task(done["id"], status="done")

    rows = await db.get_running_tasks()

    assert [row["id"] for row in rows] == [running["id"]]


@pytest.mark.asyncio
async def test_recovery_note_redacts_secrets():
    task = await db.create_task("Secret recovery", "Prompt")
    await db.update_task(task["id"], recovery_note="token=ghp_abcdefghijklmnopqrstuvwxyz123456")

    updated = await db.get_task(task["id"])
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in updated["recovery_note"]
    assert "[REDACTED]" in updated["recovery_note"]


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
async def test_recovery_migration_columns_present_after_init():
    async with db._get_db() as conn:
        rows = await (await conn.execute("PRAGMA table_info(tasks)")).fetchall()

    columns = {row["name"] for row in rows}
    assert {"branch", "current_step", "last_action", "last_tool", "recovery_note", "reconciled_at"}.issubset(columns)


@pytest.mark.asyncio
async def test_create_task_redacts_prompt_secrets():
    task = await db.create_task("Secret", "token=ghp_abcdefghijklmnopqrstuvwxyz123456")
    stored = await db.get_task(task["id"])

    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in stored["prompt"]
    assert "[REDACTED]" in stored["prompt"]


@pytest.mark.asyncio
async def test_backup_database_creates_backup(tmp_path, monkeypatch):
    from backend import config

    monkeypatch.setattr(config.settings, "backup_enabled", True)
    monkeypatch.setattr(db.settings, "backup_enabled", True)
    monkeypatch.setattr(config.settings, "backup_retention_days", 14)
    monkeypatch.setattr(db.settings, "backup_retention_days", 14)

    await db.create_task("Backup", "prompt")
    backup_path = await db.backup_database()

    assert backup_path is not None
    assert backup_path.endswith(".db")
