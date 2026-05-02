"""Workspace boundary tests for personal/work task isolation."""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend import db


@pytest.fixture
async def client():
    from backend.main import _reset_task_create_rate_limiter, _running, app

    _reset_task_create_rate_limiter()
    _running.clear()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as async_client:
        yield async_client
    for task in _running.values():
        task.cancel()
    _running.clear()
    _reset_task_create_rate_limiter()


@pytest.mark.asyncio
async def test_create_task_defaults_to_personal_workspace():
    task = await db.create_task("Personal", "prompt")

    assert task["workspace"] == "personal"
    stored = await db.get_task(task["id"])
    assert stored["workspace"] == "personal"


@pytest.mark.asyncio
async def test_db_get_task_can_filter_by_workspace():
    work = await db.create_task("Work", "prompt", workspace="work")

    assert await db.get_task(work["id"], workspace="personal") is None
    assert (await db.get_task(work["id"], workspace="work"))["id"] == work["id"]


@pytest.mark.asyncio
async def test_db_list_tasks_filters_by_workspace():
    personal = await db.create_task("Personal", "prompt", workspace="personal")
    work = await db.create_task("Work", "prompt", workspace="work")

    personal_rows = await db.list_tasks(workspace="personal")
    work_rows = await db.list_tasks(workspace="work")

    assert {row["id"] for row in personal_rows} == {personal["id"]}
    assert {row["id"] for row in work_rows} == {work["id"]}


@pytest.mark.asyncio
async def test_db_summary_filters_by_workspace():
    personal = await db.create_task("Personal", "prompt", workspace="personal")
    work = await db.create_task("Work", "prompt", workspace="work")
    await db.add_usd_spent(personal["id"], 0.25)
    await db.add_usd_spent(work["id"], 1.00)

    personal_summary = await db.get_summary(workspace="personal")
    work_summary = await db.get_summary(workspace="work")

    assert personal_summary["tasks_today"] == 1
    assert personal_summary["total_usd_today"] == pytest.approx(0.25)
    assert work_summary["tasks_today"] == 1
    assert work_summary["total_usd_today"] == pytest.approx(1.00)


@pytest.mark.asyncio
async def test_db_daily_spend_filters_by_workspace():
    personal = await db.create_task("Personal", "prompt", workspace="personal")
    work = await db.create_task("Work", "prompt", workspace="work")
    await db.add_usd_spent(personal["id"], 0.25)
    await db.add_usd_spent(work["id"], 1.00)

    assert await db.get_daily_spend(workspace="personal") == pytest.approx(0.25)
    assert await db.get_daily_spend(workspace="work") == pytest.approx(1.00)


@pytest.mark.asyncio
async def test_api_create_and_list_filter_by_workspace(client):
    with patch("backend.main.run_task", new=AsyncMock()):
        personal = await client.post("/tasks", json={"title": "Personal", "prompt": "p"})
        work = await client.post("/tasks", json={"title": "Work", "prompt": "p", "workspace": "work"})

    assert personal.status_code == 201
    assert work.status_code == 201
    assert personal.json()["workspace"] == "personal"
    assert work.json()["workspace"] == "work"

    default_list = await client.get("/tasks")
    work_list = await client.get("/tasks?workspace=work")

    assert {task["id"] for task in default_list.json()["tasks"]} == {personal.json()["id"]}
    assert {task["id"] for task in work_list.json()["tasks"]} == {work.json()["id"]}


@pytest.mark.asyncio
async def test_api_daily_budget_is_scoped_by_workspace(client, monkeypatch):
    work = await db.create_task("Work", "prompt", workspace="work")
    await db.add_usd_spent(work["id"], 1.00)
    monkeypatch.setattr("backend.main.settings.daily_max_usd", 1.00)
    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 10)

    with patch("backend.main.run_task", new=AsyncMock()):
        personal_response = await client.post("/tasks", json={"title": "Personal", "prompt": "p"})
        work_response = await client.post("/tasks", json={"title": "Work 2", "prompt": "p", "workspace": "work"})

    assert personal_response.status_code == 201
    assert work_response.status_code == 429


@pytest.mark.asyncio
async def test_api_concurrency_limit_is_scoped_by_workspace(client, monkeypatch):
    started = asyncio.Event()

    async def long_running(_task_id):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("backend.main.settings.max_concurrent_tasks", 1)
    monkeypatch.setattr("backend.main.settings.daily_max_usd", 0)
    with patch("backend.main.run_task", side_effect=long_running):
        work_response = await client.post("/tasks", json={"title": "Work", "prompt": "p", "workspace": "work"})
        await started.wait()
        personal_response = await client.post("/tasks", json={"title": "Personal", "prompt": "p"})

    assert work_response.status_code == 201
    assert personal_response.status_code == 201


@pytest.mark.asyncio
async def test_api_get_task_requires_matching_workspace(client):
    work = await db.create_task("Work", "prompt", workspace="work")

    default_response = await client.get(f"/tasks/{work['id']}")
    work_response = await client.get(f"/tasks/{work['id']}?workspace=work")

    assert default_response.status_code == 404
    assert work_response.status_code == 200
    assert work_response.json()["task"]["workspace"] == "work"


@pytest.mark.asyncio
async def test_api_retry_preserves_workspace_and_blocks_cross_workspace_retry(client):
    work = await db.create_task("Work", "prompt", workspace="work")
    await db.update_task(work["id"], status="failed", error="boom")

    wrong_workspace = await client.post(f"/tasks/{work['id']}/retry")
    with patch("backend.main.run_task", new=AsyncMock()):
        right_workspace = await client.post(f"/tasks/{work['id']}/retry?workspace=work")

    assert wrong_workspace.status_code == 404
    assert right_workspace.status_code == 201
    assert right_workspace.json()["workspace"] == "work"


@pytest.mark.asyncio
async def test_invalid_workspace_is_rejected(client):
    response = await client.post("/tasks", json={"title": "Bad", "prompt": "p", "workspace": "shared"})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_api_cancel_task_blocks_cross_workspace(client):
    work = await db.create_task("Work", "prompt", workspace="work")

    wrong_workspace = await client.delete(f"/tasks/{work['id']}")
    assert wrong_workspace.status_code == 404
    # Task should still exist after failed deletion attempt
    still_exists = await db.get_task(work["id"], workspace="work")
    assert still_exists is not None

    right_workspace = await client.delete(f"/tasks/{work['id']}?workspace=work")
    assert right_workspace.status_code == 200
    assert right_workspace.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_api_operator_run_history_filters_by_workspace(client):
    with patch("backend.main.run_task", new=AsyncMock()):
        await client.post("/tasks", json={"title": "Personal", "prompt": "p"})
        await client.post("/tasks", json={"title": "Work", "prompt": "p", "workspace": "work"})

    personal_res = await client.get("/operator/run-history?workspace=personal")
    work_res = await client.get("/operator/run-history?workspace=work")

    assert personal_res.status_code == 200
    assert work_res.status_code == 200
    personal_titles = {r["title"] for r in personal_res.json()["runs"]}
    work_titles = {r["title"] for r in work_res.json()["runs"]}
    assert "Personal" in personal_titles
    assert "Work" not in personal_titles
    assert "Work" in work_titles
    assert "Personal" not in work_titles


@pytest.mark.asyncio
async def test_api_operator_artifacts_filters_by_workspace(client):
    with patch("backend.main.run_task", new=AsyncMock()):
        await client.post("/tasks", json={"title": "Personal", "prompt": "p"})
        await client.post("/tasks", json={"title": "Work", "prompt": "p", "workspace": "work"})

    personal_res = await client.get("/operator/artifacts?workspace=personal")
    work_res = await client.get("/operator/artifacts?workspace=work")

    assert personal_res.status_code == 200
    assert work_res.status_code == 200
    personal_titles = {a["title"] for a in personal_res.json()["artifacts"]}
    work_titles = {a["title"] for a in work_res.json()["artifacts"]}
    assert "Personal" in personal_titles
    assert "Work" not in personal_titles
    assert "Work" in work_titles
    assert "Personal" not in work_titles


@pytest.mark.asyncio
async def test_api_operator_handoff_requires_matching_workspace(client):
    work = await db.create_task("Work", "prompt", workspace="work")

    wrong_workspace = await client.get(f"/operator/handoff/{work['id']}")
    right_workspace = await client.get(f"/operator/handoff/{work['id']}?workspace=work")

    assert wrong_workspace.status_code == 404
    assert right_workspace.status_code == 200
    body = right_workspace.json()
    assert body["task_id"] == work["id"]
    assert body["title"] == "Work"


@pytest.mark.asyncio
async def test_api_stream_blocks_cross_workspace(client):
    work = await db.create_task("Work", "prompt", workspace="work")

    wrong_workspace = await client.get(f"/tasks/{work['id']}/stream")
    assert wrong_workspace.status_code == 404
