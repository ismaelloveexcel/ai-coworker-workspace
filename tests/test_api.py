"""
Tests for backend.main — API auth, task creation, health endpoint.
DB isolation handled by autouse isolated_db fixture in conftest.py.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock


@pytest.fixture
async def client():
    """Async test client that bypasses lifespan (DB already init'd by isolated_db)."""
    from backend.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test"
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_ok(client):
    with patch("backend.db.health_check", new=AsyncMock(return_value=True)):
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded(client):
    with patch("backend.db.health_check", new=AsyncMock(return_value=False)):
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_create_task_no_auth(client):
    """When API_KEY is empty (default in CI), no auth required."""
    with patch("backend.agent_loop.run_task", new=AsyncMock()):
        r = await client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 201
    assert r.json()["title"] == "t"


@pytest.mark.asyncio
async def test_create_task_with_auth_required(client):
    """When API_KEY is set, requests without token get 401."""
    with patch("backend.main.settings") as ms:
        ms.api_key = "secret-token"
        ms.max_concurrent_tasks = 10
        r = await client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_task_invalid_repo_url(client):
    r = await client.post("/tasks", json={"title": "t", "prompt": "p", "repo_url": "not-valid"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_task_valid_repo_url(client):
    with patch("backend.agent_loop.run_task", new=AsyncMock()):
        r = await client.post("/tasks", json={"title": "t", "prompt": "p", "repo_url": "owner/repo"})
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_list_tasks_pagination(client):
    r = await client.get("/tasks?limit=10&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert "tasks" in data
    assert data["limit"] == 10


@pytest.mark.asyncio
async def test_get_task_not_found(client):
    r = await client.get("/tasks/nonexistent-id")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_prompt_too_long(client):
    r = await client.post("/tasks", json={"title": "t", "prompt": "x" * 8001})
    assert r.status_code == 422
