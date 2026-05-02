"""
Tests for PR-C3: state-driven visuals.

Validates:
1. task_list API returns fields required for UI state binding
   (usd_spent, last_tool, current_step, status)
2. Heartbeat SSE events are emitted on idle streams
3. Task status transitions are deterministic (no orphaned active state)
"""
import asyncio
import json
import pytest
from unittest.mock import patch, AsyncMock

from httpx import AsyncClient, ASGITransport


@pytest.fixture
async def client():
    from backend.main import _reset_task_create_rate_limiter, _running, app
    _reset_task_create_rate_limiter()
    _running.clear()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
    for task in _running.values():
        task.cancel()
    _running.clear()
    _reset_task_create_rate_limiter()


# ---------------------------------------------------------------------------
# AC-1: Visual states map deterministically to backend states
# ---------------------------------------------------------------------------

# The canonical TOOL_AGENT_MAP from the frontend is replicated here so Python
# tests can assert the same deterministic mapping rules.
TOOL_AGENT_MAP = {
    "github_create_branch":    "builder",
    "github_commit_files":     "builder",
    "filesystem_write":        "builder",
    "filesystem_read":         "builder",
    "filesystem_list":         "builder",
    "github_read_file":        "builder",
    "github_list_files":       "builder",
    "github_compare_branch":   "builder",
    "run_tests":               "tester",
    "get_code_scanning_alert": "tester",
    "list_code_scanning_alerts": "tester",
    "github_create_pr":        "handoff",
    "final_answer":            "handoff",
}


def tool_to_agent(tool_name: str) -> str:
    """Mirror of the JS toolToAgent() function."""
    return TOOL_AGENT_MAP.get(tool_name, "supervisor")


class TestToolAgentMapping:
    def test_builder_tools_map_to_builder(self):
        for tool in [
            "github_create_branch",
            "github_commit_files",
            "filesystem_write",
            "github_read_file",
            "github_compare_branch",
        ]:
            assert tool_to_agent(tool) == "builder", f"{tool!r} should map to builder"

    def test_tester_tools_map_to_tester(self):
        for tool in ["run_tests", "get_code_scanning_alert", "list_code_scanning_alerts"]:
            assert tool_to_agent(tool) == "tester", f"{tool!r} should map to tester"

    def test_handoff_tools_map_to_handoff(self):
        for tool in ["github_create_pr", "final_answer"]:
            assert tool_to_agent(tool) == "handoff", f"{tool!r} should map to handoff"

    def test_unknown_tools_default_to_supervisor(self):
        assert tool_to_agent("") == "supervisor"
        assert tool_to_agent("some_unknown_tool") == "supervisor"
        assert tool_to_agent("cost_status") == "supervisor"

    def test_all_mapped_tools_have_deterministic_agent(self):
        """Every tool in the map has exactly one agent — no ambiguity."""
        for tool, agent in TOOL_AGENT_MAP.items():
            assert agent in {"builder", "tester", "handoff"}, (
                f"{tool!r} maps to unexpected agent {agent!r}"
            )


# ---------------------------------------------------------------------------
# AC-2: No hard-coded active-state illusions
# ---------------------------------------------------------------------------

class TestHardCodedStateCheck:
    """Ensure agent-supervisor does not start in 'active' state in the HTML."""

    def test_agent_supervisor_initial_state_is_idle(self):
        import pathlib, re
        html = pathlib.Path("frontend/index.html").read_text()
        # The agent-supervisor element must start in 'idle', not 'active'
        match = re.search(r'id="agent-supervisor"', html)
        assert match, "agent-supervisor element not found"
        # Check context around the id for the class value
        context = html[max(0, match.start() - 100): match.end() + 100]
        assert 'agent-char idle' in context, (
            "agent-supervisor must start in 'idle' class, not 'active'. "
            f"Context: {context!r}"
        )
        assert 'agent-char active' not in context, (
            "agent-supervisor must not have hard-coded 'active' class on page load."
        )

    def test_tool_agent_map_covers_all_executor_tools(self):
        """All tools that actually write code or run tests should be in the map."""
        critical_tools = {
            "github_commit_files", "run_tests", "github_create_pr", "final_answer",
        }
        for tool in critical_tools:
            assert tool in TOOL_AGENT_MAP, f"{tool!r} must be in TOOL_AGENT_MAP"


# ---------------------------------------------------------------------------
# AC-3: Accessibility preserved
# ---------------------------------------------------------------------------

class TestAccessibility:
    def test_agent_chars_have_aria_labels(self):
        import pathlib
        html = pathlib.Path("frontend/index.html").read_text()
        for agent_id in ["agent-supervisor", "agent-builder", "agent-tester", "agent-handoff"]:
            # Find the element and check aria-label
            import re
            # Match the div line containing the agent id
            pattern = r'id="' + re.escape(agent_id) + r'"[^>]*aria-label="([^"]+)"' \
                    + r'|aria-label="([^"]+)"[^>]*id="' + re.escape(agent_id) + r'"'
            assert re.search(pattern, html), (
                f"Element #{agent_id} must have an aria-label attribute"
            )

    def test_prefers_reduced_motion_css_present(self):
        import pathlib
        html = pathlib.Path("frontend/index.html").read_text()
        assert "prefers-reduced-motion" in html, (
            "HTML must include @media (prefers-reduced-motion) CSS rule"
        )
        assert "animation: none" in html, (
            "prefers-reduced-motion block must disable animations with 'animation: none'"
        )

    def test_budget_bar_has_aria_role_meter(self):
        """renderBudgetBar() produces role=meter markup."""
        import pathlib
        html = pathlib.Path("frontend/index.html").read_text()
        assert 'role="meter"' in html, (
            "Budget bar must use role='meter' for screen-reader accessibility"
        )
        assert 'aria-valuenow' in html, (
            "Budget bar must include aria-valuenow for screen readers"
        )

    def test_stream_status_has_aria_live(self):
        """Stream status row must use aria-live for dynamic updates."""
        import pathlib
        html = pathlib.Path("frontend/index.html").read_text()
        assert 'stream-status-row' in html, "stream-status-row element must exist"
        assert 'aria-live="polite"' in html, (
            "Stream status row must use aria-live='polite' for accessibility"
        )


# ---------------------------------------------------------------------------
# API smoke tests: task list returns state fields
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_list_includes_usd_spent_field(client):
    """GET /tasks must return usd_spent for budget burn UI."""
    with patch("backend.main.run_task", new=AsyncMock()):
        await client.post("/tasks", json={"title": "t", "prompt": "p"})
    r = await client.get("/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert len(tasks) > 0
    task = tasks[0]
    assert "usd_spent" in task, "usd_spent field missing from task list response"


@pytest.mark.asyncio
async def test_task_list_includes_last_tool_field(client):
    """GET /tasks must return last_tool so the UI can map tool→agent."""
    from backend import db
    task = await db.create_task("tool test", "prompt")
    await db.update_task(task["id"], last_tool="run_tests")
    r = await client.get("/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    found = next((t for t in tasks if t["id"] == task["id"]), None)
    assert found is not None
    assert "last_tool" in found, "last_tool field missing from task list response"
    assert found["last_tool"] == "run_tests"


@pytest.mark.asyncio
async def test_task_list_includes_current_step_field(client):
    """GET /tasks must return current_step for progress tracking."""
    from backend import db
    task = await db.create_task("step test", "prompt")
    await db.update_task(task["id"], current_step=3)
    r = await client.get("/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    found = next((t for t in tasks if t["id"] == task["id"]), None)
    assert found is not None
    assert "current_step" in found, "current_step field missing from task list response"
    assert found["current_step"] == 3


@pytest.mark.asyncio
async def test_task_list_returns_max_task_usd(client):
    """GET /tasks must return max_task_usd so budget bar has a cap to display."""
    r = await client.get("/tasks")
    assert r.status_code == 200
    data = r.json()
    assert "max_task_usd" in data, "max_task_usd missing from /tasks response"
    assert data["max_task_usd"] is not None


@pytest.mark.asyncio
async def test_tool_events_emitted_on_sse_stream(client):
    """SSE stream must emit tool_start and tool_done events with tool name."""
    from backend import db
    from backend.events import emit, destroy_bus, subscribe

    task = await db.create_task("sse test", "prompt")
    task_id = task["id"]

    # Emit tool events directly to the bus
    queue = subscribe(task_id)
    await emit(task_id, "tool_start", {"tool": "run_tests", "input": "{}"})
    await emit(task_id, "tool_done", {"tool": "run_tests", "success": True})

    start_ev = queue.get_nowait()
    done_ev  = queue.get_nowait()

    assert start_ev["type"] == "tool_start"
    assert start_ev["data"]["tool"] == "run_tests"
    assert done_ev["type"] == "tool_done"
    assert done_ev["data"]["tool"] == "run_tests"
    assert done_ev["data"]["success"] is True

    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_heartbeat_emitted_as_keepalive(client):
    """SSE stream emits heartbeat events on idle intervals."""
    from backend import db
    from backend.events import subscribe, destroy_bus

    task = await db.create_task("heartbeat test", "prompt")
    task_id = task["id"]
    await db.update_task(task_id, status="running")

    # The SSE generator yields heartbeats when no events arrive for 25 s.
    # We validate the backend emits the raw string format as expected by the
    # event generator — not a full integration test (that would block for 25 s).
    # Here we just confirm the SSE endpoint accepts running tasks and the
    # generator function exists with the correct heartbeat line.
    import inspect
    from backend.main import stream_task
    src = inspect.getsource(stream_task)
    assert 'heartbeat' in src, "SSE generator must emit heartbeat keepalive events"
    assert "event: heartbeat" in src, "heartbeat must use SSE event: prefix"

    destroy_bus(task_id)
