"""Tests for operator studio recipes, artifacts, run history, and handoff APIs."""
from httpx import ASGITransport, AsyncClient
import pytest

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
async def test_recipe_catalog_lists_preview_only_recipes(client):
    response = await client.get("/operator/recipes")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    assert "general" in data["categories"]
    assert all(recipe["editable"] is False for recipe in data["recipes"])


@pytest.mark.asyncio
async def test_recipe_catalog_filters_by_category(client):
    response = await client.get("/operator/recipes?category=work")

    assert response.status_code == 200
    data = response.json()
    assert data["count"] >= 1
    assert {recipe["category"] for recipe in data["recipes"]} == {"work"}


@pytest.mark.asyncio
async def test_operator_artifacts_are_derived_from_tasks(client):
    task = await db.create_task("Completed", "prompt")
    await db.update_task(task["id"], status="done", pr_url="https://github.com/example/repo/pull/1", last_tool="github_create_pr")

    response = await client.get("/operator/artifacts")

    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert artifacts[0]["id"] == f"task:{task['id']}"
    assert artifacts[0]["artifact_type"] == "handoff_summary"
    assert artifacts[0]["trace"]["pr_url"] == "https://github.com/example/repo/pull/1"


@pytest.mark.asyncio
async def test_operator_run_history_summarizes_recent_tasks(client):
    task = await db.create_task("Run", "prompt")
    await db.update_task(task["id"], status="running", current_step=2, last_action="tool_call")

    response = await client.get("/operator/run-history")

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert runs[0]["task_id"] == task["id"]
    assert runs[0]["current_step"] == 2
    assert runs[0]["last_action"] == "tool_call"


@pytest.mark.asyncio
async def test_operator_handoff_is_traceable(client):
    task = await db.create_task("Handoff", "prompt")
    await db.update_task(task["id"], status="failed", error="boom", branch="task/handoff", last_tool="run_tests")
    step_id = await db.create_step(task["id"], 1, tool_name="run_tests")
    await db.update_step(step_id, status="failed", tool_output="tests failed")
    await db.add_log(task["id"], "error", "tests failed")

    response = await client.get(f"/operator/handoff/{task['id']}")

    assert response.status_code == 200
    data = response.json()
    assert data["artifact_type"] == "handoff_summary"
    assert data["task_id"] == task["id"]
    assert "Status: failed" in data["summary"]
    assert data["trace"]["branch"] == "task/handoff"
    assert data["trace"]["last_tool"] == "run_tests"
    assert data["trace"]["log_count"] == 1


@pytest.mark.asyncio
async def test_operator_handoff_missing_task_returns_404(client):
    response = await client.get("/operator/handoff/missing")

    assert response.status_code == 404
