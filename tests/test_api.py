"""
Tests for backend.main — API auth, task creation, health endpoint.
Uses httpx AsyncClient with FastAPI testclient pattern.
"""
import pytest
import os

os.environ["DB_PATH"] = ":memory:"
os.environ["ANTHROPIC_API_KEY"] = "sk-dummy"
os.environ["GH_PAT"] = "ghp-dummy"

from httpx import AsyncClient, ASGITransport
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.fixture
async def client():
    from backend.main import app
    from backend import db
    await db.init_db()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_health_ok(client):
    with patch("backend.db.health_check", return_value=True):
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_health_degraded(client):
    with patch("backend.db.health_check", return_value=False):
        r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"


@pytest.mark.asyncio
async def test_create_task_no_auth(client):
    """When API_KEY is empty, no auth required."""
    with patch("backend.agent_loop.run_task", new_callable=AsyncMock), \
         patch("backend.agent_loop._running", {}):
        r = await client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 201
    assert r.json()["title"] == "t"


@pytest.mark.asyncio
async def test_create_task_with_auth_required(client):
    """When API_KEY is set, requests without token are rejected."""
    with patch("backend.config.settings") as mock_settings:
        mock_settings.api_key = "secret-token"
        r = await client.post("/tasks", json={"title": "t", "prompt": "p"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_create_task_invalid_repo_url(client):
    r = await client.post("/tasks", json={"title": "t", "prompt": "p", "repo_url": "not-valid"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_task_valid_repo_url(client):
    with patch("backend.agent_loop.run_task", new_callable=AsyncMock), \
         patch("backend.agent_loop._running", {}):
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

