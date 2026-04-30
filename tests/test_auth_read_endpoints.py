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
