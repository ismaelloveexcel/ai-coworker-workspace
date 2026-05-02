"""
Tests for PR-C1: GET /tasks/{task_id}/steps/{step_id} — tool I/O inspectability.

Validates:
- Happy path: returns full step detail.
- 404 when task or step not found.
- Safe size bounds: fields larger than _STEP_MAX_FIELD_CHARS are truncated
  and listed in ``_truncated_fields``.
- Redaction is preserved (write-time redaction flows through unchanged).
- Workspace scoping: cross-workspace requests return 404.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock


@pytest.fixture
async def client():
    from backend.main import _reset_task_create_rate_limiter, _running, app
    _reset_task_create_rate_limiter()
    _running.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    for task in _running.values():
        task.cancel()
    _running.clear()
    _reset_task_create_rate_limiter()


@pytest.mark.asyncio
async def test_get_step_detail_happy_path(client):
    from backend import db

    task = await db.create_task("Step detail test", "prompt")
    step_id = await db.create_step(task["id"], 0, tool_name="run_tests", tool_input='{"suite":"quick"}')
    await db.update_step(step_id, status="done", tool_output='{"success": true, "output": "All tests passed"}')

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}")

    assert r.status_code == 200
    data = r.json()
    assert data["id"] == step_id
    assert data["task_id"] == task["id"]
    assert data["tool_name"] == "run_tests"
    assert data["status"] == "done"
    assert "tool_input" in data
    assert "tool_output" in data
    assert "_truncated_fields" not in data


@pytest.mark.asyncio
async def test_get_step_detail_task_not_found(client):
    r = await client.get("/tasks/nonexistent-task-id/steps/nonexistent-step-id")
    assert r.status_code == 404
    assert r.json()["detail"] == "Task not found"


@pytest.mark.asyncio
async def test_get_step_detail_step_not_found(client):
    from backend import db

    task = await db.create_task("Step not found", "prompt")
    r = await client.get(f"/tasks/{task['id']}/steps/nonexistent-step-id")
    assert r.status_code == 404
    assert r.json()["detail"] == "Step not found"


@pytest.mark.asyncio
async def test_get_step_detail_step_from_wrong_task_returns_404(client):
    """Step scoped to task_a must not be retrievable under task_b."""
    from backend import db

    task_a = await db.create_task("Task A", "prompt")
    task_b = await db.create_task("Task B", "prompt")
    step_id = await db.create_step(task_a["id"], 0, tool_name="run_tests")

    r = await client.get(f"/tasks/{task_b['id']}/steps/{step_id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_step_detail_truncates_large_tool_output(client, monkeypatch):
    """Fields exceeding _STEP_MAX_FIELD_CHARS must be truncated."""
    from backend import db
    import backend.main as main_mod

    # Patch the limit to a small value for testing
    monkeypatch.setattr(main_mod, "_STEP_MAX_FIELD_CHARS", 20)

    task = await db.create_task("Truncation test", "prompt")
    step_id = await db.create_step(task["id"], 0, tool_name="filesystem_read",
                                   tool_input='{"path":"/tmp/f"}')
    # Use a realistic-looking output that won't be redacted
    big_output = "All tests passed. Step completed normally. " * 5
    await db.update_step(step_id, status="done", tool_output=big_output)

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}")

    assert r.status_code == 200
    data = r.json()
    assert len(data["tool_output"]) == 20
    assert "tool_output" in data["_truncated_fields"]


@pytest.mark.asyncio
async def test_get_step_detail_no_truncation_when_within_limit(client, monkeypatch):
    """Fields within the limit must not appear in _truncated_fields."""
    from backend import db
    import backend.main as main_mod

    monkeypatch.setattr(main_mod, "_STEP_MAX_FIELD_CHARS", 200)

    task = await db.create_task("Within limit", "prompt")
    step_id = await db.create_step(task["id"], 0, tool_name="run_tests")
    small_output = "step output: build succeeded"  # well within 200 chars
    await db.update_step(step_id, status="done", tool_output=small_output)

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}")

    assert r.status_code == 200
    data = r.json()
    assert data["tool_output"] == small_output
    assert "_truncated_fields" not in data


@pytest.mark.asyncio
async def test_get_step_detail_redaction_preserved(client):
    """Secrets written to the DB at write-time must remain redacted in responses."""
    from backend import db

    task = await db.create_task("Redaction test", "prompt")
    step_id = await db.create_step(task["id"], 0, tool_name="github_read_file")
    # write a value with a secret token — db.update_step redacts it at write time
    await db.update_step(
        step_id,
        status="done",
        tool_output='{"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"}',
    )

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}")

    assert r.status_code == 200
    payload = r.text
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in payload


@pytest.mark.asyncio
async def test_get_step_detail_workspace_scoping(client):
    """Task in workspace='work' must not be reachable with workspace='personal'."""
    from backend import db

    task = await db.create_task("Work task", "prompt", workspace="work")
    step_id = await db.create_step(task["id"], 0, tool_name="run_tests")

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}?workspace=personal")
    assert r.status_code == 404
    assert r.json()["detail"] == "Task not found"


@pytest.mark.asyncio
async def test_get_step_detail_no_auth_required_when_key_unset(client):
    """When API_KEY is empty, the endpoint is accessible without a token."""
    from backend import db

    task = await db.create_task("Open access", "prompt")
    step_id = await db.create_step(task["id"], 0, tool_name="cost_status")

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_get_step_detail_auth_required_when_key_set(client, monkeypatch):
    """When API_KEY is set, missing token must return 401."""
    from backend import db

    monkeypatch.setattr("backend.main.settings.api_key", "test-secret")
    task = await db.create_task("Auth required", "prompt")
    step_id = await db.create_step(task["id"], 0, tool_name="cost_status")

    r = await client.get(f"/tasks/{task['id']}/steps/{step_id}")
    assert r.status_code == 401
