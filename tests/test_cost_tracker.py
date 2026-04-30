"""
Tests for backend.cost_tracker — BudgetExceeded cap and cost maths.
DB isolation handled by autouse isolated_db fixture in conftest.py.
"""
import pytest

from backend.cost_tracker import BudgetExceeded, PRICES, calculate_cost


# ---------------------------------------------------------------------------
# Pure cost maths (no DB needed)
# ---------------------------------------------------------------------------

def test_calculate_cost_known_model():
    # 1 M input @ $3 + 1 M output @ $15 = $18
    cost = calculate_cost("claude-sonnet-4-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(18.00)


def test_calculate_cost_only_input_tokens():
    cost = calculate_cost("claude-sonnet-4-5", 1_000_000, 0)
    assert cost == pytest.approx(3.00)


def test_calculate_cost_only_output_tokens():
    cost = calculate_cost("claude-sonnet-4-5", 0, 1_000_000)
    assert cost == pytest.approx(15.00)


def test_calculate_cost_unknown_model_uses_default():
    default_cost = calculate_cost("_default", 1_000_000, 0)
    unknown_cost = calculate_cost("some-future-model", 1_000_000, 0)
    assert unknown_cost == pytest.approx(default_cost)


def test_calculate_cost_zero_tokens():
    assert calculate_cost("claude-sonnet-4-5", 0, 0) == pytest.approx(0.0)


def test_all_price_entries_have_required_keys():
    for model, pricing in PRICES.items():
        assert "input" in pricing, f"Missing 'input' key for model {model!r}"
        assert "output" in pricing, f"Missing 'output' key for model {model!r}"


# ---------------------------------------------------------------------------
# record_and_check — requires an isolated DB (from conftest autouse fixture)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_budget_not_exceeded_under_cap(monkeypatch):
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 100.0)
    task = await db.create_task("T", "P")

    total = await cost_tracker.record_and_check(task["id"], "claude-sonnet-4-5", 100, 100)
    assert total > 0
    assert total < 100.0

    # Verify persisted in DB
    row = await db.get_task(task["id"])
    assert row["usd_spent"] == pytest.approx(total)


@pytest.mark.asyncio
async def test_budget_exceeded_raises(monkeypatch):
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 0.001)
    task = await db.create_task("T", "P")

    with pytest.raises(BudgetExceeded) as exc_info:
        # 1 M input + 1 M output at sonnet prices = $18 >> $0.001 cap
        await cost_tracker.record_and_check(task["id"], "claude-sonnet-4-5", 1_000_000, 1_000_000)

    exc = exc_info.value
    assert exc.task_id == task["id"]
    assert exc.spent > exc.cap


@pytest.mark.asyncio
async def test_budget_accumulated_across_calls(monkeypatch):
    """Spend is cumulative; cap is enforced on the running total."""
    from backend import config, db
    from backend import cost_tracker

    # Cap just above one tiny call but below two
    monkeypatch.setattr(config.settings, "max_task_usd", 0.01)
    task = await db.create_task("T", "P")

    # First call — well under cap (100 tokens each ≈ $0.0000018)
    await cost_tracker.record_and_check(task["id"], "claude-sonnet-4-5", 100, 100)

    # Second call with huge tokens — should exceed cap
    with pytest.raises(BudgetExceeded):
        await cost_tracker.record_and_check(task["id"], "claude-sonnet-4-5", 10_000_000, 0)
