"""
GitHub-issue failure notifier.

Opens a labelled issue in the target repo when a task fails.
Uses the existing PyGithub client from tool_adapters — no new HTTP client.
"""
import asyncio
from typing import List, Optional

import structlog
from github import GithubException
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

log = structlog.get_logger(__name__)


def _is_transient_or_rate_limited(exc: Exception) -> bool:
    """Retry on transient GitHub server errors (5xx) *and* rate-limit responses (403/429).

    GitHub returns 403 for secondary rate limits and 429 for primary rate
    limits.  Both are safe to retry with backoff.
    """
    if not isinstance(exc, GithubException):
        return False
    if exc.status >= 500:
        return True
    if exc.status in (403, 429):
        # Distinguish rate-limit 403s from genuine permission errors by
        # inspecting the response message/data that PyGithub exposes.
        data = exc.data or {}
        message = (data.get("message") or "").lower()
        return "rate limit" in message or "secondary rate" in message or exc.status == 429
    return False

MAX_LOG_LINES = 50


async def notify_task_failure(
    task_id: str,
    repo: str,
    error: str,
    logs_url: Optional[str] = None,
) -> None:
    """
    Open a GitHub issue in *repo* reporting the task failure.

    Fetches the last MAX_LOG_LINES log entries from the DB, then delegates
    the actual HTTP call to a thread so the async event loop is not blocked.
    Failures are logged as warnings — a notification error must not crash
    the caller.
    """
    from backend import db

    try:
        # Fetch all logs then take the tail so we always get the *most recent* lines
        all_logs = await db.get_logs(task_id, limit=500)
        logs = all_logs[-MAX_LOG_LINES:]
    except Exception:
        logs = []

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            None, _create_issue, task_id, repo, error, logs_url, logs
        )
    except Exception as exc:
        log.warning("notify_task_failure_error", task_id=task_id, error=str(exc))


# ---------------------------------------------------------------------------
# Sync helper (runs in a thread executor)
# ---------------------------------------------------------------------------

def _create_issue(
    task_id: str,
    repo: str,
    error: str,
    logs_url: Optional[str],
    logs: List[dict],
) -> None:
    """Create a GitHub issue synchronously (called from a thread executor)."""
    from backend.tool_adapters import _get_repo, _redact  # reuse existing client

    import re as _re
    # Normalize whitespace/newlines so the title stays on a single line
    safe_error = _redact(error or "unknown")
    short_error = _re.sub(r"\s+", " ", safe_error.strip())[:120]
    title = f"[ai-coworker] Task {task_id[:8]} failed: {short_error}"

    log_lines = [
        _redact(f"[{entry.get('level', 'info').upper()}] {entry.get('message', '')}")
        for entry in logs[-MAX_LOG_LINES:]
    ]
    log_tail = "\n".join(log_lines) or "(no logs available)"

    body_parts = [
        "## Task Failure Report",
        "",
        f"**Task ID:** `{task_id}`",
        f"**Repo:** `{repo}`",
        f"**Error:** {safe_error}",
    ]
    if logs_url:
        body_parts.append(f"**Logs:** {logs_url}")
    body_parts += [
        "",
        f"## Last {MAX_LOG_LINES} Log Lines",
        "```",
        log_tail,
        "```",
    ]
    body = "\n".join(body_parts)

    try:
        r = _get_repo(repo)
        _gh_create_issue(r, title=title, body=body, labels=["bug"])
        log.info("notify_issue_created", task_id=task_id, repo=repo)
    except Exception as exc:
        log.warning("notify_create_issue_failed", task_id=task_id, repo=repo, error=str(exc))


@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4), retry=retry_if_exception(_is_transient_or_rate_limited))
def _gh_create_issue(repo, title: str, body: str, labels: list) -> None:
    repo.create_issue(title=title, body=body, labels=labels)
