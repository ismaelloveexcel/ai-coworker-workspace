"""
H3: Authenticate read & SSE endpoints.

When API_KEY is set, GET /tasks, GET /tasks/{id}, and GET /tasks/{id}/stream
must require a valid Bearer token. /health stays unauthenticated.
"""
import pytest
from httpx import AsyncClient, ASGITransport


_API_KEY = "test-secret-key"


@pytest.fixture
async def auth_client(monkeypatch):
    """Client with API_KEY set, lifespan bypassed (DB init'd by isolated_db)."""
    from backend import config
    from backend.main import app
    monkeypatch.setattr(config.settings, "api_key", _API_KEY)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_remains_unauthenticated(auth_client):
    """Docker healthcheck depends on /health being open."""
    r = await auth_client.get("/health")
    assert r.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/tasks",
    "/tasks/some-id",
    "/tasks/some-id/stream",
])
async def test_read_endpoints_reject_missing_token(auth_client, path):
    r = await auth_client.get(path)
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/tasks",
    "/tasks/some-id",
    "/tasks/some-id/stream",
])
async def test_read_endpoints_reject_wrong_token(auth_client, path):
    r = await auth_client.get(path, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_list_tasks_accepts_correct_token(auth_client):
    r = await auth_client.get("/tasks", headers={"Authorization": f"Bearer {_API_KEY}"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_operator_status_requires_bearer_token(auth_client):
    missing = await auth_client.get("/operator/status")
    query = await auth_client.get(f"/operator/status?token={_API_KEY}")
    good = await auth_client.get("/operator/status", headers={"Authorization": f"Bearer {_API_KEY}"})

    assert missing.status_code == 401
    assert query.status_code == 401
    assert good.status_code == 200


@pytest.mark.asyncio
async def test_operator_backup_requires_bearer_token(auth_client):
    missing = await auth_client.post("/operator/backup")
    query = await auth_client.post(f"/operator/backup?token={_API_KEY}")

    assert missing.status_code == 401
    assert query.status_code == 401


@pytest.mark.asyncio
async def test_query_param_token_rejected_for_non_sse_read_endpoints(auth_client):
    from backend import db

    task = await db.create_task("query-token", "prompt")

    list_response = await auth_client.get(f"/tasks?token={_API_KEY}")
    task_response = await auth_client.get(f"/tasks/{task['id']}?token={_API_KEY}")
    summary_response = await auth_client.get(f"/summary?token={_API_KEY}")

    assert list_response.status_code == 401
    assert task_response.status_code == 401
    assert summary_response.status_code == 401


@pytest.mark.asyncio
async def test_query_param_token_rejected_for_non_sse_mutating_endpoints(auth_client):
    from backend import db

    task = await db.create_task("query-token", "prompt")
    await db.update_task(task["id"], status="failed", error="boom")

    create_response = await auth_client.post(
        f"/tasks?token={_API_KEY}",
        json={"title": "t", "prompt": "p"},
    )
    delete_response = await auth_client.delete(f"/tasks/{task['id']}?token={_API_KEY}")
    retry_response = await auth_client.post(f"/tasks/{task['id']}/retry?token={_API_KEY}")

    assert create_response.status_code == 401
    assert delete_response.status_code == 401
    assert retry_response.status_code == 401


@pytest.mark.asyncio
async def test_stream_accepts_token_query_param(auth_client):
    """EventSource cannot send headers; SSE endpoint must accept ?token= query param."""
    from backend import db
    from unittest.mock import AsyncMock, patch
    from starlette.requests import Request

    task = await db.create_task("sse-auth-test", "prompt")
    # Patch is_disconnected to avoid httpx ASGITransport deadlock in test context.
    # httpx's receive() blocks on asyncio.Event.wait() once the request body is
    # consumed, and the SSE generator blocks on is_disconnected() calling receive().
    # Returning True immediately lets the generator close after auth passes.
    with patch.object(Request, "is_disconnected", AsyncMock(return_value=True)):
        r = await auth_client.get(f"/tasks/{task['id']}/stream?token={_API_KEY}")
        assert r.status_code == 200
    # Wrong token via query param still rejects (auth fails before stream starts)
    r2 = await auth_client.get(f"/tasks/{task['id']}/stream?token=wrong")
    assert r2.status_code == 401


@pytest.mark.asyncio
async def test_get_task_accepts_correct_token(auth_client):
    from backend import db
    task = await db.create_task("auth-test", "prompt")
    r = await auth_client.get(
        f"/tasks/{task['id']}",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_stream_task_accepts_correct_token(auth_client):
    """SSE endpoint with valid token reaches the handler (404 for unknown task)."""
    r = await auth_client.get(
        "/tasks/missing/stream",
        headers={"Authorization": f"Bearer {_API_KEY}"},
    )
    # Not 401 — auth passed; handler returns 404 because task does not exist.
    assert r.status_code == 404
