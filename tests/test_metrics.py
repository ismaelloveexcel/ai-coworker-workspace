"""
Tests for PR-A1 metrics foundation.

Verifies:
- get_metrics() aggregation math on seeded data
- success_rate / failure_rate calculation (excluding cancelled)
- latency stats (median/p95) from started_at/ended_at fields
- failure_category distribution
- cost summary
- /metrics API endpoint returns expected shape
- Backward-compatibility: existing task flow still works without new fields
"""
import pytest
from datetime import datetime, timedelta, timezone

from backend import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(offset_minutes: int = 0) -> str:
    """Return an ISO-8601 UTC timestamp offset by given minutes from now."""
    return (datetime.now(timezone.utc) - timedelta(minutes=offset_minutes)).isoformat()


async def _seed_task(status: str, failure_category: str = None,
                     usd_spent: float = 0.0,
                     duration_minutes: float = None) -> str:
    """Create and update a task with the given metrics fields."""
    task = await db.create_task("Seed", "prompt")
    tid = task["id"]
    kwargs = {"status": status, "usd_spent": usd_spent}
    if failure_category:
        kwargs["failure_category"] = failure_category
    if duration_minutes is not None:
        started = _ts(offset_minutes=duration_minutes + 5)
        ended = _ts(offset_minutes=5)
        kwargs["started_at"] = started
        kwargs["ended_at"] = ended
    await db.update_task(tid, **kwargs)
    return tid


# ---------------------------------------------------------------------------
# Schema / migration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_columns_present_after_migration():
    async with db._get_db() as conn:
        rows = await (await conn.execute("PRAGMA table_info(tasks)")).fetchall()
    columns = {row["name"] for row in rows}
    assert {"started_at", "ended_at", "failure_category"}.issubset(columns)


@pytest.mark.asyncio
async def test_new_columns_updatable():
    task = await db.create_task("T", "P")
    now = datetime.now(timezone.utc).isoformat()
    await db.update_task(task["id"],
                         started_at=now,
                         ended_at=now,
                         failure_category="agent_error")
    updated = await db.get_task(task["id"])
    assert updated["started_at"] == now
    assert updated["ended_at"] == now
    assert updated["failure_category"] == "agent_error"


@pytest.mark.asyncio
async def test_backward_compat_create_task_no_timing_fields():
    """Tasks created without timing fields should have NULL values (not error)."""
    task = await db.create_task("Old style", "prompt")
    stored = await db.get_task(task["id"])
    assert stored["started_at"] is None
    assert stored["ended_at"] is None
    assert stored["failure_category"] is None


# ---------------------------------------------------------------------------
# get_metrics() aggregation math
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_empty_db():
    result = await db.get_metrics(window_hours=24)
    assert result["total_tasks"] == 0
    assert result["succeeded"] == 0
    assert result["failed"] == 0
    assert result["cancelled"] == 0
    assert result["success_rate"] is None
    assert result["failure_rate"] is None
    assert result["latency"]["count"] == 0
    assert result["cost_summary"]["total_usd"] == 0.0
    assert result["cost_summary"]["avg_usd_per_task"] is None


@pytest.mark.asyncio
async def test_metrics_success_rate_math():
    """success_rate = done / (done + failed); cancelled excluded from denominator."""
    await _seed_task("done")
    await _seed_task("done")
    await _seed_task("failed", failure_category="agent_error")
    await _seed_task("cancelled")  # no failure_category for cancelled tasks

    result = await db.get_metrics(window_hours=24)
    assert result["total_tasks"] == 4
    assert result["succeeded"] == 2
    assert result["failed"] == 1
    assert result["cancelled"] == 1
    # success_rate = 2 / (2+1) ≈ 0.6667
    assert abs(result["success_rate"] - 2/3) < 0.001
    # failure_rate = 1 / (2+1) ≈ 0.3333
    assert abs(result["failure_rate"] - 1/3) < 0.001


@pytest.mark.asyncio
async def test_metrics_only_cancelled_tasks():
    """When all tasks are cancelled, rates should be None (no non-cancelled finished tasks)."""
    await _seed_task("cancelled")  # no failure_category for cancelled
    result = await db.get_metrics(window_hours=24)
    assert result["success_rate"] is None
    assert result["failure_rate"] is None


@pytest.mark.asyncio
async def test_metrics_latency_stats():
    """Latency should be computed from started_at/ended_at fields."""
    # 3 tasks with durations: 10min, 20min, 30min → median=20, p95≈30
    await _seed_task("done", duration_minutes=10)
    await _seed_task("done", duration_minutes=20)
    await _seed_task("done", duration_minutes=30)

    result = await db.get_metrics(window_hours=24)
    latency = result["latency"]
    assert latency["count"] == 3
    # median should be ~1200s (20min)
    assert 1100 <= latency["median_s"] <= 1300
    # p95 should be close to 1800s (30min)
    assert latency["p95_s"] is not None


@pytest.mark.asyncio
async def test_metrics_latency_excludes_tasks_without_timing():
    """Tasks without started_at/ended_at should not affect latency stats."""
    # One task with timing
    await _seed_task("done", duration_minutes=5)
    # One task without timing (old-style)
    await _seed_task("done")

    result = await db.get_metrics(window_hours=24)
    assert result["latency"]["count"] == 1


@pytest.mark.asyncio
async def test_metrics_failure_category_distribution():
    """failure_categories should tally failed tasks by category."""
    await _seed_task("failed", failure_category="timeout")
    await _seed_task("failed", failure_category="timeout")
    await _seed_task("failed", failure_category="budget_exceeded")
    await _seed_task("done")  # should not appear in categories

    result = await db.get_metrics(window_hours=24)
    cats = result["failure_categories"]
    assert cats["timeout"] == 2
    assert cats["budget_exceeded"] == 1
    assert "done" not in cats


@pytest.mark.asyncio
async def test_metrics_failure_category_unknown_for_missing():
    """Failed task without failure_category falls into 'unknown' bucket."""
    task = await db.create_task("Old", "prompt")
    await db.update_task(task["id"], status="failed")  # no failure_category

    result = await db.get_metrics(window_hours=24)
    assert result["failure_categories"].get("unknown", 0) >= 1


@pytest.mark.asyncio
async def test_metrics_cost_summary():
    """cost_summary totals usd_spent and computes per-task average."""
    await _seed_task("done", usd_spent=0.10)
    await _seed_task("done", usd_spent=0.20)
    await _seed_task("failed", failure_category="agent_error", usd_spent=0.05)

    result = await db.get_metrics(window_hours=24)
    cs = result["cost_summary"]
    assert abs(cs["total_usd"] - 0.35) < 1e-5
    assert abs(cs["avg_usd_per_task"] - 0.35 / 3) < 1e-5


@pytest.mark.asyncio
async def test_metrics_window_isolation():
    """Tasks older than the window should not appear in results.

    We seed one task and then query with window_hours=24 (should include it)
    and also verify that a task created 'in the past' via direct SQL is excluded
    when querying a small window.
    """
    # Seed a normal (in-window) task
    await _seed_task("failed", failure_category="agent_error")

    # Insert an old task directly so we can control its created_at timestamp
    old_ts = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    async with db._get_db() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, title, prompt, status, created_at, updated_at, workspace) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old-task-id", "Old", "prompt", "done", old_ts, old_ts, "personal"),
        )

    # 24h window should include the recent failed task but not the 50h-old done task
    result_24h = await db.get_metrics(window_hours=24)
    assert result_24h["total_tasks"] == 1
    assert result_24h["failed"] == 1

    # 72h window should include both
    result_72h = await db.get_metrics(window_hours=72)
    assert result_72h["total_tasks"] == 2


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_endpoint_returns_expected_shape():
    from httpx import AsyncClient, ASGITransport
    from backend.main import app, _reset_task_create_rate_limiter, _running
    _reset_task_create_rate_limiter()
    _running.clear()

    await _seed_task("done", usd_spent=0.05)
    await _seed_task("failed", failure_category="timeout")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/metrics")

    assert r.status_code == 200
    data = r.json()
    # Default window is 24h
    assert data["window_hours"] == 24
    assert "success_rate" in data
    assert "failure_rate" in data
    assert "latency" in data
    assert "failure_categories" in data
    assert "cost_summary" in data
    assert data["succeeded"] == 1
    assert data["failed"] == 1


@pytest.mark.asyncio
async def test_metrics_endpoint_custom_window():
    from httpx import AsyncClient, ASGITransport
    from backend.main import app, _reset_task_create_rate_limiter, _running
    _reset_task_create_rate_limiter()
    _running.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/metrics?window=48")

    assert r.status_code == 200
    data = r.json()
    assert data["window_hours"] == 48


@pytest.mark.asyncio
async def test_metrics_endpoint_rejects_invalid_window():
    from httpx import AsyncClient, ASGITransport
    from backend.main import app, _reset_task_create_rate_limiter, _running
    _reset_task_create_rate_limiter()
    _running.clear()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r = await ac.get("/metrics?window=0")

    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Latency helper unit tests
# ---------------------------------------------------------------------------

def test_latency_stats_empty():
    from backend.db import _latency_stats
    result = _latency_stats([])
    assert result == {"median_s": None, "p95_s": None, "count": 0}


def test_latency_stats_single():
    from backend.db import _latency_stats
    result = _latency_stats([60.0])
    assert result["count"] == 1
    assert result["median_s"] == 60.0
    assert result["p95_s"] == 60.0


def test_latency_stats_even_count():
    from backend.db import _latency_stats
    # [10, 20] → median = (10+20)/2 = 15; p95 = 20 (nearest-rank: ceil(2*0.95)-1 = 1 → sorted[1])
    result = _latency_stats([10.0, 20.0])
    assert result["median_s"] == 15.0
    assert result["p95_s"] == 20.0


def test_latency_stats_p95():
    from backend.db import _latency_stats
    # 20 values: 1..20; p95 index = int(20*0.95)-1 = 18 → value[18]=19
    durations = [float(i) for i in range(1, 21)]
    result = _latency_stats(durations)
    assert result["count"] == 20
    assert result["median_s"] == 10.5
    assert result["p95_s"] == 19.0
