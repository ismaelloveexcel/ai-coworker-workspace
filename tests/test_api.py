"""
Tests for backend.main — API auth, task creation, health endpoint.
DB isolation handled by autouse isolated_db fixture in conftest.py.
"""
import pytest
import asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock


@pytest.fixture
async def client():
    """Async test client that bypasses lifespan (DB already init'd by isolated_db)."""
    from backend.main import _reset_task_create_rate_limiter, _running, app
    _reset_task_create_rate_limiter()
    _running.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac
    for task in _running.values():
        task.cancel()
    _running.clear()
    _reset_task_create_rate_limiter()


@pytest.mark.asyncio
async def test_health_ok(client):
    with patch("backend.db.health_detail", new=AsyncMock(return_value={"ok": True})):
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded(client):
    with patch("backend.db.health_detail", new=AsyncMock(return_value={"ok": False})):
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_create_task_no_auth(client):
    """When API_KEY is empty (default in CI), no auth required."""
    with patch("backend.main.run_task", new=AsyncMock()):
        r = await client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 201
    assert r.json()["title"] == "t"


@pytest.mark.asyncio
async def test_create_task_with_auth_required(client, monkeypatch):
    """When API_KEY is set, requests without token get 401."""
    monkeypatch.setattr("backend.main.settings.api_key", "secret-token")
    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 10)
    r = await client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_task_invalid_repo_url(client):
    r = await client.post("/tasks", json={"title": "t", "prompt": "p", "repo_url": "not-valid"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_task_valid_repo_url(client):
    with patch("backend.main.run_task", new=AsyncMock()):
        r = await client.post("/tasks", json={"title": "t", "prompt": "p", "repo_url": "owner/repo"})
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_create_task_rejects_when_concurrency_limit_reached(client, monkeypatch):
    started = asyncio.Event()

    async def long_running(_task_id):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 1)
    with patch("backend.main.run_task", side_effect=long_running):
        first = await client.post("/tasks", json={"title": "one", "prompt": "p"})
        await started.wait()
        second = await client.post("/tasks", json={"title": "two", "prompt": "p"})

    assert first.status_code == 201
    assert second.status_code == 409


@pytest.mark.asyncio
async def test_create_task_concurrent_requests_respect_concurrency_lock(client, monkeypatch):
    started = asyncio.Event()

    async def long_running(_task_id):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 1)
    with patch("backend.main.run_task", side_effect=long_running):
        responses = await asyncio.gather(
            client.post("/tasks", json={"title": "one", "prompt": "p"}),
            client.post("/tasks", json={"title": "two", "prompt": "p"}),
        )

    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409]


@pytest.mark.asyncio
async def test_create_task_rate_limit_rejects_second_request(client, monkeypatch):
    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 10)
    monkeypatch.setattr("backend.main.settings.task_create_rate_limit_count", 1)
    monkeypatch.setattr("backend.main.settings.task_create_rate_limit_window_seconds", 60)

    with patch("backend.main.run_task", new=AsyncMock()):
        first = await client.post("/tasks", json={"title": "one", "prompt": "p"})
        second = await client.post("/tasks", json={"title": "two", "prompt": "p"})

    assert first.status_code == 201
    assert second.status_code == 429
    assert "rate limit" in second.json()["detail"].lower()


@pytest.mark.asyncio
async def test_auth_failure_does_not_consume_task_rate_limit(client, monkeypatch):
    monkeypatch.setattr("backend.main.settings.api_key", "secret-token")
    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 10)
    monkeypatch.setattr("backend.main.settings.task_create_rate_limit_count", 1)
    monkeypatch.setattr("backend.main.settings.task_create_rate_limit_window_seconds", 60)

    first_bad = await client.post("/tasks", json={"title": "bad", "prompt": "p"})
    second_bad = await client.post("/tasks", json={"title": "bad2", "prompt": "p"})
    with patch("backend.main.run_task", new=AsyncMock()):
        good = await client.post(
            "/tasks",
            json={"title": "good", "prompt": "p"},
            headers={"Authorization": "Bearer secret-token"},
        )

    assert first_bad.status_code == 401
    assert second_bad.status_code == 401
    assert good.status_code == 201


@pytest.mark.asyncio
async def test_create_task_rejects_oversized_request_body(client, monkeypatch):
    monkeypatch.setattr("backend.main.settings.task_request_max_bytes", 20)

    response = await client.post("/tasks", json={"title": "large", "prompt": "payload"})

    assert response.status_code == 413


@pytest.mark.asyncio
async def test_retry_failed_task_creates_new_task(client):
    from backend import db

    task = await db.create_task("failed", "prompt")
    await db.update_task(task["id"], status="failed", error="boom")

    with patch("backend.main.run_task", new=AsyncMock()):
        r = await client.post(f"/tasks/{task['id']}/retry")

    assert r.status_code == 201
    assert r.json()["title"].startswith("Retry:")


@pytest.mark.asyncio
async def test_retry_failed_task_rate_limit_rejects_second_retry(client, monkeypatch):
    from backend import db

    task = await db.create_task("failed", "prompt")
    await db.update_task(task["id"], status="failed", error="boom")
    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 10)
    monkeypatch.setattr("backend.main.settings.task_create_rate_limit_count", 1)
    monkeypatch.setattr("backend.main.settings.task_create_rate_limit_window_seconds", 60)

    with patch("backend.main.run_task", new=AsyncMock()):
        first = await client.post(f"/tasks/{task['id']}/retry")
        second = await client.post(f"/tasks/{task['id']}/retry")

    assert first.status_code == 201
    assert second.status_code == 429


@pytest.mark.asyncio
async def test_list_tasks_pagination(client):
    r = await client.get("/tasks?limit=10&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert "tasks" in data
    assert data["limit"] == 10


@pytest.mark.asyncio
async def test_get_task_includes_recovery_fields(client):
    from backend import db

    task = await db.create_task("Recoverable", "prompt")
    await db.update_task(
        task["id"],
        branch="task/recoverable",
        current_step=2,
        last_action="tool_call",
        last_tool="github_commit_files",
        recovery_note="Review branch task/recoverable",
        reconciled_at="2026-05-02T00:00:00+00:00",
    )

    r = await client.get(f"/tasks/{task['id']}")

    assert r.status_code == 200
    returned = r.json()["task"]
    assert returned["branch"] == "task/recoverable"
    assert returned["current_step"] == 2
    assert returned["last_action"] == "tool_call"
    assert returned["last_tool"] == "github_commit_files"
    assert returned["recovery_note"] == "Review branch task/recoverable"
    assert returned["reconciled_at"] == "2026-05-02T00:00:00+00:00"


@pytest.mark.asyncio
async def test_two_sse_streams_for_same_task_receive_event():
    from backend import db
    from backend.events import _subscribers, destroy_bus, emit
    from backend.main import stream_task

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    task = await db.create_task("SSE fanout", "prompt")
    first = await stream_task(task["id"], ConnectedRequest())
    second = await stream_task(task["id"], ConnectedRequest())

    await emit(task["id"], "task_done", {"ok": True})

    first_chunk = await first.body_iterator.__anext__()
    second_chunk = await second.body_iterator.__anext__()

    if isinstance(first_chunk, bytes):
        first_chunk = first_chunk.decode()
    if isinstance(second_chunk, bytes):
        second_chunk = second_chunk.decode()

    assert "event: task_done" in first_chunk
    assert "event: task_done" in second_chunk
    assert '"ok": true' in first_chunk
    assert '"ok": true' in second_chunk

    await first.body_iterator.aclose()
    await second.body_iterator.aclose()
    assert task["id"] not in _subscribers
    destroy_bus(task["id"])


@pytest.mark.asyncio
async def test_get_task_not_found(client):
    r = await client.get("/tasks/nonexistent-id")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_prompt_too_long(client):
    r = await client.post("/tasks", json={"title": "t", "prompt": "x" * 8001})
    assert r.status_code == 422
