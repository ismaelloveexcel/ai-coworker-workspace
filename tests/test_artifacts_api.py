import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(monkeypatch):
    from backend import config
    from backend.main import _running, app

    monkeypatch.setattr(config.settings, "api_key", "")
    monkeypatch.setattr(config.settings, "insecure_local_auth", True)
    _running.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    for task in _running.values():
        task.cancel()
    _running.clear()


@pytest.mark.asyncio
async def test_create_task_defaults_workspace(client, monkeypatch):
    monkeypatch.setattr("backend.main.run_task", AsyncMock())

    response = await client.post("/tasks", json={"title": "t", "prompt": "p"})

    assert response.status_code == 201
    assert response.json()["workspace"] == "personal"


@pytest.mark.asyncio
async def test_agent_recipe_and_supervisor_endpoints(client):
    agents = await client.get("/agents?workspace=work")
    recipes = await client.get("/recipes?workspace=personal")
    plan = await client.post("/supervisor/plan", json={"prompt": "research competitors", "workspace": "personal"})

    assert agents.status_code == 200
    assert any(agent["id"] == "crm_specialist" for agent in agents.json()["agents"])
    assert recipes.status_code == 200
    assert plan.status_code == 200
    assert plan.json()["recipe"]["id"] == "market_research"


@pytest.mark.asyncio
async def test_artifact_create_list_get_and_version(client):
    from backend import db

    task = await db.create_task("Artifact task", "prompt", workspace="work")
    create = await client.post(
        "/artifacts",
        json={
            "task_id": task["id"],
            "workspace": "work",
            "artifact_type": "research_brief",
            "title": "Brief",
            "content": {"summary": "first"},
            "created_by": "researcher",
        },
    )

    assert create.status_code == 201
    artifact_id = create.json()["id"]
    listed = await client.get(f"/artifacts?task_id={task['id']}&workspace=work")
    fetched = await client.get(f"/artifacts/{artifact_id}?workspace=work")
    updated = await client.post(
        f"/artifacts/{artifact_id}/versions?workspace=work",
        json={"content": {"summary": "second"}, "created_by": "critic", "change_note": "reviewed"},
    )

    assert listed.status_code == 200
    assert listed.json()["artifacts"][0]["id"] == artifact_id
    assert fetched.status_code == 200
    assert json.loads(fetched.json()["versions"][0]["content_json"])["summary"] == "first"
    assert updated.status_code == 200
    assert updated.json()["current_version"] == 2


@pytest.mark.asyncio
async def test_artifact_create_rejects_unknown_task(client):
    response = await client.post(
        "/artifacts",
        json={
            "task_id": "missing",
            "workspace": "personal",
            "artifact_type": "document",
            "title": "Doc",
            "content": {},
        },
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_artifact_create_rejects_invalid_artifact_type(client):
    response = await client.post(
        "/artifacts",
        json={
            "workspace": "personal",
            "artifact_type": "unknown",
            "title": "Doc",
            "content": {},
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_artifact_create_rejects_invalid_workspace(client):
    response = await client.post(
        "/artifacts",
        json={
            "workspace": "shared",
            "artifact_type": "document",
            "title": "Doc",
            "content": {},
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_artifact_read_and_version_require_matching_workspace(client):
    create = await client.post(
        "/artifacts",
        json={
            "workspace": "personal",
            "artifact_type": "document",
            "title": "Personal doc",
            "content": {"summary": "private"},
        },
    )
    artifact_id = create.json()["id"]

    missing_workspace = await client.get(f"/artifacts/{artifact_id}")
    wrong_workspace = await client.get(f"/artifacts/{artifact_id}?workspace=work")
    wrong_version = await client.post(
        f"/artifacts/{artifact_id}/versions?workspace=work",
        json={"content": {"summary": "cross"}},
    )

    assert missing_workspace.status_code == 422
    assert wrong_workspace.status_code == 404
    assert wrong_version.status_code == 404


@pytest.mark.asyncio
async def test_artifact_content_redacts_secrets(client):
    response = await client.post(
        "/artifacts",
        json={
            "workspace": "personal",
            "artifact_type": "document",
            "title": "Secret doc",
            "content": {"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"},
        },
    )

    assert response.status_code == 201
    content = response.json()["versions"][0]["content_json"]
    assert "ghp_abcdefghijklmnopqrstuvwxyz123456" not in content
    assert "[REDACTED]" in content
