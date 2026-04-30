"""
GitHub-issue failure notifier.

Opens a labelled issue in the target repo when a task fails.
Uses the existing PyGithub client from tool_adapters — no new HTTP client.
"""
import asyncio
from typing import List, Optional

import structlog

log = structlog.get_logger(__name__)

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
    from backend.tool_adapters import _get_repo  # reuse existing client

    import re as _re
    # Normalize whitespace/newlines so the title stays on a single line
    short_error = _re.sub(r"\s+", " ", (error or "unknown error").strip())[:120]
    title = f"[ai-coworker] Task {task_id[:8]} failed: {short_error}"

    log_lines = [
        f"[{entry.get('level', 'info').upper()}] {entry.get('message', '')}"
        for entry in logs[-MAX_LOG_LINES:]
    ]
    log_tail = "\n".join(log_lines) or "(no logs available)"

    body_parts = [
        "## Task Failure Report",
        "",
        f"**Task ID:** `{task_id}`",
        f"**Repo:** `{repo}`",
        f"**Error:** {error or 'unknown'}",
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
        r.create_issue(title=title, body=body, labels=["bug"])
        log.info("notify_issue_created", task_id=task_id, repo=repo)
    except Exception as exc:
        log.warning("notify_create_issue_failed", task_id=task_id, repo=repo, error=str(exc))
