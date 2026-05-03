"""Optional webhook notifications when tasks reach a terminal state (Area 4)."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

import httpx
import structlog

from backend import db
from backend.config import settings

log = structlog.get_logger(__name__)


def _post_sync(url: str, body: Dict[str, Any]) -> None:
    try:
        httpx.post(url, content=json.dumps(body), headers={"Content-Type": "application/json"}, timeout=10.0)
    except Exception as exc:
        log.warning("webhook_post_failed", error=str(exc))


async def schedule_terminal_webhook(task_id: str, terminal_status: str) -> None:
    """POST to ``WEBHOOK_URL`` for ``done`` or ``failed`` tasks (best-effort)."""
    if terminal_status not in ("done", "failed"):
        return
    url = (settings.webhook_url or "").strip()
    if not url:
        return
    task = await db.get_task(task_id)
    if not task:
        return
    body = {
        "task_id": task.get("id"),
        "status": terminal_status,
        "title": task.get("title"),
        "workspace": task.get("workspace"),
        "pr_url": task.get("pr_url"),
        "usd_spent": task.get("usd_spent"),
        "failure_category": task.get("failure_category"),
        "error": (task.get("error") or "")[:2000] if terminal_status == "failed" else None,
    }
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _post_sync, url, body)


def fire_terminal_webhook(task_id: str, terminal_status: str) -> None:
    """Schedule webhook without blocking the agent loop."""
    if terminal_status not in ("done", "failed"):
        return
    if not (settings.webhook_url or "").strip():
        return
    try:
        asyncio.create_task(schedule_terminal_webhook(task_id, terminal_status))
    except RuntimeError:
        pass
