"""
Per-task USD cost tracking with a configurable hard cap.

Token prices in USD per 1 million tokens.
Keep this dict up-to-date when Anthropic changes pricing:
  https://www.anthropic.com/pricing
"""

from backend.config import settings

from typing import Dict

# ---------------------------------------------------------------------------
# Price table — USD per 1 M tokens (input / output)
# ---------------------------------------------------------------------------

PRICES: Dict[str, Dict[str, float]] = {
    # Claude Sonnet 4 family
    "claude-sonnet-4-5":              {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-5-20251101":     {"input": 3.00,  "output": 15.00},
    # Claude 3.5 Sonnet
    "claude-3-5-sonnet-20241022":     {"input": 3.00,  "output": 15.00},
    "claude-3-5-sonnet-20240620":     {"input": 3.00,  "output": 15.00},
    # Claude 3 Opus
    "claude-3-opus-20240229":         {"input": 15.00, "output": 75.00},
    # Claude 3.5 Haiku / Claude 3 Haiku
    "claude-haiku-3-5":               {"input": 0.80,  "output": 4.00},
    "claude-3-5-haiku-20241022":      {"input": 0.80,  "output": 4.00},
    "claude-3-haiku-20240307":        {"input": 0.25,  "output": 1.25},
    # Fallback — matches claude-sonnet tier; update if you change the default model
    "_default":                       {"input": 3.00,  "output": 15.00},
}


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """Raised when a task's cumulative spend exceeds MAX_TASK_USD."""

    def __init__(self, task_id: str, spent: float, cap: float) -> None:
        self.task_id = task_id
        self.spent = spent
        self.cap = cap
        super().__init__(
            f"Task {task_id} exceeded budget: ${spent:.4f} spent > ${cap:.2f} cap"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for one API call."""
    pricing = PRICES.get(model, PRICES["_default"])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


async def record_and_check(
    task_id: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """
    Persist this call's cost to the task row and check against the cap.

    Returns the new cumulative spend.
    Raises BudgetExceeded if the cap is exceeded.
    """
    from backend import db  # local import avoids circular at module level

    cost = calculate_cost(model, input_tokens, output_tokens)
    new_total = await db.add_usd_spent(task_id, cost)

    cap = settings.max_task_usd
    if new_total > cap:
        raise BudgetExceeded(task_id, new_total, cap)

    return new_total
