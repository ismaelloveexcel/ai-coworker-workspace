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
async def test_operator_status_returns_private_dashboard_shape(client):
    from backend import db

    running = await db.create_task("Running", "prompt", "owner/repo")
    failed = await db.create_task("Failed", "prompt", "owner/repo")
    await db.update_task(
        running["id"],
        status="running",
        branch="task/running",
        current_step=4,
        last_action="tool_call",
        last_tool="github_read_file",
        recovery_note="Review branch task/running",
        pr_url="https://github.com/owner/repo/pull/2",
    )
    await db.update_task(failed["id"], status="failed", error="boom")

    response = await client.get("/operator/status")

    assert response.status_code == 200
    data = response.json()
    assert set(data) == {"app", "db", "tasks", "spend", "backup", "guardrails"}
    assert data["app"]["name"] == "AI Coworker"
    assert data["tasks"]["counts_by_status"]["running"] == 1
    assert data["tasks"]["counts_by_status"]["failed"] == 1
    assert data["tasks"]["running"][0]["id"] == running["id"]
    assert data["tasks"]["running"][0]["repo"] == "owner/repo"
    assert data["tasks"]["running"][0]["branch"] == "task/running"
    assert data["tasks"]["running"][0]["current_step"] == 4
    assert data["tasks"]["running"][0]["last_action"] == "tool_call"
    assert data["tasks"]["running"][0]["last_tool"] == "github_read_file"
    assert "prompt" not in data["tasks"]["running"][0]
    assert data["tasks"]["recent_failed"][0]["id"] == failed["id"]
    assert data["guardrails"]["task_request_max_bytes"] > 0


@pytest.mark.asyncio
async def test_operator_status_redacts_task_errors_and_recovery_notes(client):
    from backend import db

    task = await db.create_task("Secret failure", "prompt")
    await db.update_task(
        task["id"],
        status="failed",
        error="token=ghp_abcdefghijklmnopqrstuvwxyz123456",
        recovery_note="token=ghp_abcdefghijklmnopqrstuvwxyz123456",
    )

    response = await client.get("/operator/status")

    assert response.status_code == 200
    payload = response.text
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in payload
    assert "[REDACTED]" in payload


@pytest.mark.asyncio
async def test_operator_status_does_not_expose_secret_config_values(client, monkeypatch):
    monkeypatch.setattr("backend.main.settings.api_key", "super-secret-api-key")
    monkeypatch.setattr("backend.main.settings.github_token", "ghp_abcdefghijklmnopqrstuvwxyz123456")
    monkeypatch.setattr("backend.main.settings.anthropic_api_key", "sk-ant-super-secret")

    response = await client.get(
        "/operator/status",
        headers={"Authorization": "Bearer super-secret-api-key"},
    )

    assert response.status_code == 200
    payload = response.text
    assert "super-secret-api-key" not in payload
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in payload
    assert "sk-ant-super-secret" not in payload
    assert response.json()["app"]["auth_required"] is True


@pytest.mark.asyncio
async def test_operator_backup_triggers_local_backup(client, tmp_path, monkeypatch):
    from backend import config, db

    monkeypatch.setattr(config.settings, "backup_enabled", True)
    monkeypatch.setattr(db.settings, "backup_enabled", True)
    monkeypatch.setattr(config.settings, "backup_retention_days", 14)
    monkeypatch.setattr(db.settings, "backup_retention_days", 14)

    await db.create_task("Backup", "prompt")
    response = await client.post("/operator/backup")

    assert response.status_code == 200
    data = response.json()
    assert data["created"] is True
    assert data["backup"]["enabled"] is True
    assert data["backup"]["backup_count"] >= 1


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
    first = await stream_task(task["id"], ConnectedRequest(), workspace="personal")
    second = await stream_task(task["id"], ConnectedRequest(), workspace="personal")

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
    # A2a: sequence ID should appear as SSE id: field
    assert "id: " in first_chunk
    assert "id: " in second_chunk

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


@pytest.mark.asyncio
async def test_sse_stream_includes_seq_id_line():
    """SSE events must include an 'id: <seq>' line for client-side sequence tracking."""
    from backend import db
    from backend.events import destroy_bus, emit
    from backend.main import stream_task

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    task = await db.create_task("SSE seq test", "prompt")
    resp = await stream_task(task["id"], ConnectedRequest(), workspace="personal")

    await emit(task["id"], "log", {"message": "hello"})
    await emit(task["id"], "task_done", {})

    chunks = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        chunks.append(chunk)
        if "task_done" in chunk:
            break

    body = "".join(chunks)
    # SSE id: line should be present with a numeric sequence
    assert "id: " in body, "SSE stream must include id: line with sequence number"
    await resp.body_iterator.aclose()
    destroy_bus(task["id"])


@pytest.mark.asyncio
async def test_sse_stream_warning_event_appears_on_overflow():
    """stream_warning event must appear in SSE stream when queue overflows."""
    from backend import db
    from backend.events import MAX_QUEUE, destroy_bus, emit
    from backend.main import stream_task

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    task = await db.create_task("SSE overflow test", "prompt")
    resp = await stream_task(task["id"], ConnectedRequest(), workspace="personal")

    # Flood the queue to trigger overflow
    for i in range(MAX_QUEUE + 2):
        await emit(task["id"], "log", {"i": i})
    await emit(task["id"], "task_done", {})

    chunks = []
    async for chunk in resp.body_iterator:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        chunks.append(chunk)
        if "task_done" in chunk or "stream_warning" in chunk:
            break

    body = "".join(chunks)
    assert "stream_warning" in body, "Expected stream_warning event in SSE output after overflow"
    await resp.body_iterator.aclose()
    destroy_bus(task["id"])

