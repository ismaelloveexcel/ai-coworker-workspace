"""
Tests for backend.cost_tracker — BudgetExceeded cap and cost maths.
DB isolation handled by autouse isolated_db fixture in conftest.py.
"""
import pytest

from backend.cost_tracker import BudgetExceeded, BudgetPreflightError, PRICES, calculate_cost, estimate_input_tokens


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
# estimate_input_tokens — pure function, no DB
# ---------------------------------------------------------------------------

def test_estimate_input_tokens_simple_string():
    messages = [{"role": "user", "content": "abcd"}]  # 4 chars → 1 token
    tokens = estimate_input_tokens(messages)
    assert tokens == 1


def test_estimate_input_tokens_ceiling():
    # 5 chars → ceil(5/4) = 2 tokens
    messages = [{"role": "user", "content": "abcde"}]
    tokens = estimate_input_tokens(messages)
    assert tokens == 2


def test_estimate_input_tokens_empty_messages():
    assert estimate_input_tokens([]) == 0


def test_estimate_input_tokens_empty_content():
    messages = [{"role": "user", "content": ""}]
    assert estimate_input_tokens(messages) == 0


def test_estimate_input_tokens_adds_system_tokens():
    messages = [{"role": "user", "content": "abcd"}]  # 1 token
    tokens = estimate_input_tokens(messages, system_tokens=100)
    assert tokens == 101


def test_estimate_input_tokens_content_block_list():
    """Handles Anthropic content-block list format."""
    messages = [{"role": "user", "content": [{"type": "text", "text": "abcd"}]}]
    tokens = estimate_input_tokens(messages)
    assert tokens == 1


def test_estimate_input_tokens_multiple_messages():
    messages = [
        {"role": "user", "content": "aaaa"},      # 4 chars → 1 token
        {"role": "assistant", "content": "bbbb"},  # 4 chars → 1 token
    ]
    assert estimate_input_tokens(messages) == 2


def test_estimate_input_tokens_large_context():
    """Estimate is always positive for substantial inputs."""
    messages = [{"role": "user", "content": "x" * 40_000}]
    tokens = estimate_input_tokens(messages)
    assert tokens == 10_000


def test_estimate_input_tokens_system_tokens_only():
    """Zero message content: only system tokens contribute."""
    assert estimate_input_tokens([], system_tokens=512) == 512


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


# ---------------------------------------------------------------------------
# preflight_check — requires an isolated DB
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preflight_allows_call_within_budget(monkeypatch):
    """preflight_check does not raise when there is sufficient remaining budget."""
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 5.00)
    task = await db.create_task("T", "P")

    # 1000 input + 1000 output on sonnet ≈ $0.000018 — well within $5.00 cap
    await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000, 1_000)


@pytest.mark.asyncio
async def test_preflight_refuses_when_budget_exhausted(monkeypatch):
    """preflight_check raises BudgetPreflightError when estimated cost > remaining."""
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 0.001)
    task = await db.create_task("T", "P")

    with pytest.raises(BudgetPreflightError) as exc_info:
        # 1 M input = $3.00 >> $0.001 remaining
        await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000_000, 0)

    exc = exc_info.value
    assert exc.task_id == task["id"]
    assert exc.estimated > exc.remaining


@pytest.mark.asyncio
async def test_preflight_error_is_budget_exceeded_subclass(monkeypatch):
    """BudgetPreflightError is caught by handlers that catch BudgetExceeded."""
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 0.0001)
    task = await db.create_task("T", "P")

    with pytest.raises(BudgetExceeded):
        await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000_000, 0)


@pytest.mark.asyncio
async def test_preflight_does_not_write_to_db(monkeypatch):
    """preflight_check must NOT persist anything — accounting stays in record_and_check."""
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 5.00)
    task = await db.create_task("T", "P")

    await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000, 1_000)

    row = await db.get_task(task["id"])
    assert (row["usd_spent"] or 0.0) == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_preflight_respects_accumulated_spend(monkeypatch):
    """Preflight considers existing spend accumulated by previous calls."""
    from backend import config, db
    from backend import cost_tracker

    # Cap = $0.01; first real call eats $0.0000018 ≈ 0
    monkeypatch.setattr(config.settings, "max_task_usd", 0.01)
    task = await db.create_task("T", "P")

    # Simulate a cheap previous real call (records $0.009 of spend)
    await db.add_usd_spent(task["id"], 0.009)

    # Now only $0.001 remains; preflight for 1M tokens ($3.00) should refuse
    with pytest.raises(BudgetPreflightError):
        await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000_000, 0)


@pytest.mark.asyncio
async def test_preflight_boundary_exact_remaining(monkeypatch):
    """preflight_check allows call when estimated cost exactly equals remaining budget."""
    from backend import config, db
    from backend import cost_tracker

    # Set cap exactly equal to the cost of a 1M-input call ($3.00)
    monkeypatch.setattr(config.settings, "max_task_usd", 3.00)
    task = await db.create_task("T", "P")

    # Estimated cost = $3.00; remaining = $3.00 — should be allowed (not strictly >)
    await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000_000, 0)


@pytest.mark.asyncio
async def test_preflight_boundary_one_cent_over(monkeypatch):
    """preflight_check refuses when estimated cost exceeds remaining by even one cent."""
    from backend import config, db
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 3.00)
    task = await db.create_task("T", "P")

    # $0.01 already spent, so remaining = $2.99; estimated = $3.00 → refuse
    await db.add_usd_spent(task["id"], 0.01)

    with pytest.raises(BudgetPreflightError):
        await cost_tracker.preflight_check(task["id"], "claude-sonnet-4-5", 1_000_000, 0)


@pytest.mark.asyncio
async def test_preflight_unknown_task_allows_call(monkeypatch):
    """If the task row cannot be found, preflight allows the call to proceed
    so that record_and_check can enforce the cap after the fact."""
    from backend import config
    from backend import cost_tracker

    monkeypatch.setattr(config.settings, "max_task_usd", 0.001)

    # Should NOT raise even though budget would be tiny
    await cost_tracker.preflight_check("nonexistent-id", "claude-sonnet-4-5", 1_000_000, 0)
