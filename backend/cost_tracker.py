"""
Per-task USD cost tracking with a configurable hard cap.

Token prices in USD per 1 million tokens.
Keep this dict up-to-date when Anthropic changes pricing:
  https://www.anthropic.com/pricing
"""

from backend.config import settings

from typing import Dict, List

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
# Exceptions
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


class BudgetPreflightError(BudgetExceeded):
    """Raised *before* an API call when the estimated cost would exceed the remaining budget.

    Subclasses BudgetExceeded so existing ``except BudgetExceeded`` handlers
    catch preflight refusals automatically.
    """

    def __init__(self, task_id: str, estimated: float, remaining: float, cap: float) -> None:
        self.task_id = task_id
        self.estimated = estimated
        self.remaining = remaining
        self.cap = cap
        # Call Exception.__init__ directly to avoid BudgetExceeded's different signature.
        # We still set self.spent for compatibility with handlers that access it.
        Exception.__init__(
            self,
            f"Task {task_id} budget preflight refused: "
            f"estimated ${estimated:.4f} > remaining ${remaining:.4f} "
            f"(cap ${cap:.2f})"
        )
        # Keep parent attrs consistent for handlers that read exc.spent / exc.cap
        self.spent = cap - remaining + estimated


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def calculate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return the USD cost for one API call."""
    pricing = PRICES.get(model, PRICES["_default"])
    return (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000


def estimate_input_tokens(messages: List[Dict], system_tokens: int = 0) -> int:
    """Rough pre-call estimate of input tokens.

    Uses the industry-standard heuristic of ~4 characters per token for
    Latin-script text.  The estimate is intentionally conservative
    (rounds *up*) so the preflight guard errs on the side of caution.

    Args:
        messages: list of Anthropic-format message dicts (``{"role": ..., "content": ...}``).
        system_tokens: pre-computed token count for the system prompt, if available.

    Returns:
        Estimated total input-token count (int).
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Content blocks: [{type: "text", text: "..."}, ...]
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(block.get("text", ""))
                else:
                    total_chars += len(str(block))
    # 4 chars ≈ 1 token; ceiling division to be conservative
    message_tokens = (total_chars + 3) // 4
    return message_tokens + system_tokens


async def preflight_check(
    task_id: str,
    model: str,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
) -> None:
    """Refuse the next call when the estimated cost exceeds the remaining budget.

    This is a *pre-call* guard: it reads the current spend from the DB but
    does **not** write anything — post-call ``record_and_check`` remains the
    source of truth for actual accounting.

    Raises:
        BudgetPreflightError: if estimated cost > remaining budget.
    """
    from backend import db  # local import avoids circular at module level

    task = await db.get_task(task_id)
    if task is None:
        # Cannot estimate remaining budget — let the call proceed and let
        # record_and_check enforce the cap afterwards.
        return

    cap = settings.max_task_usd
    spent = float(task.get("usd_spent") or 0.0)
    remaining = cap - spent

    estimated_cost = calculate_cost(model, estimated_input_tokens, estimated_output_tokens)

    if estimated_cost > remaining:
        raise BudgetPreflightError(task_id, estimated_cost, remaining, cap)


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
