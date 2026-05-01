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
async def test_stream_accepts_token_query_param(auth_client):
    """EventSource cannot send headers; SSE endpoint must accept ?token= query param."""
    from backend import db
    from backend.events import emit
    from unittest.mock import AsyncMock, patch
    from starlette.requests import Request

    task = await db.create_task("sse-auth-test", "prompt")
    # Pre-emit a terminal event so the stream closes immediately once auth passes.
    await emit(task["id"], "task_done", {})
    # Patch is_disconnected to avoid httpx ASGITransport deadlock in test context.
    # httpx's receive() blocks on asyncio.Event.wait() once the request body is
    # consumed, and the SSE generator blocks on is_disconnected() calling receive().
    # Returning False immediately lets the generator consume the pre-emitted
    # task_done event and exit cleanly.
    with patch.object(Request, "is_disconnected", AsyncMock(return_value=False)):
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
