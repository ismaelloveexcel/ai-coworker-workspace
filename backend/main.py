"""
FastAPI application — AI Coworker backend.

v2 fixes:
- F3/E1:  Optional Bearer token auth on all endpoints (mutating + read/SSE);
          /health is intentionally left unauthenticated for Docker healthchecks
- F30/E4: SSE generator detects client disconnect (request.is_disconnected)
- F45:    /health pings the DB, not just returns OK
- F29:    GET /tasks supports ?limit=&offset= pagination
- F33:    repo_url validated as owner/repo format
- F51:    Removed duplicate uvicorn uvloop policy set (uvicorn handles it)
- F52:    structlog configured once at startup
"""
import asyncio
import os as _os
import re
from contextlib import asynccontextmanager
from typing import Optional

import structlog
import structlog.stdlib
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, field_validator

from backend import db
from backend.agent_loop import _running, run_task
from backend.config import settings
from backend.events import get_bus
from backend.notifier import notify_task_failure


# ---------------------------------------------------------------------------
# Logging setup (F52) — configure once at import time
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.contextvars.merge_contextvars,
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.processors.JSONRenderer() if settings.log_json else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

_configure_logging()
log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", db_path=settings.db_path)
    if not settings.api_key:
        log.warning(
            "api_key_not_set",
            message="API_KEY is empty — all mutating endpoints are unauthenticated. "
                    "Set API_KEY in any networked deployment.",
        )
    await db.init_db()
    await _reap_zombie_tasks()
    yield
    log.info("shutdown")


async def _reap_zombie_tasks() -> None:
    """Mark stale running tasks as failed and open a GitHub issue for each."""
    zombies = await db.get_zombie_tasks()
    for task in zombies:
        task_id = task["id"]
        repo = task.get("repo_url") or settings.github_default_repo
        reason = "zombie task reaped on restart"
        await db.update_task(task_id, status="failed", error=reason)
        log.warning("zombie_reaped", task_id=task_id, repo=repo)
        try:
            await notify_task_failure(task_id, repo, reason)
        except Exception as exc:
            log.warning("zombie_notify_failed", task_id=task_id, error=str(exc))


# ---------------------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------------------

# F3/F28: restrict CORS to explicit origins via API_CORS_ORIGINS env var.
# Defaults to localhost only. Set to "*" explicitly only for fully-public APIs.
_cors_origins = [o.strip() for o in _os.environ.get("API_CORS_ORIGINS", "http://localhost,http://localhost:8000").split(",") if o.strip()]

app = FastAPI(title="AI Coworker", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)


# ---------------------------------------------------------------------------
# Auth dependency (F3/E1)
# ---------------------------------------------------------------------------

async def require_auth(request: Request) -> None:
    """If API_KEY is set, validate Bearer token. No-op when API_KEY is empty."""
    if not settings.api_key:
        return  # unauthenticated mode (localhost dev)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != settings.api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid or missing Bearer token")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

# Patterns that look like secrets/tokens — redact them from PR titles.
# Matches 20+ consecutive base64url/hex chars that resemble API keys or tokens.
_SECRET_RE = re.compile(r"[A-Za-z0-9_\-]{20,}")
_PR_TITLE_MAX = 72  # GitHub renders ~72 chars before truncating in the UI


def _sanitize_title(title: str) -> str:
    """
    Sanitize a user-supplied task title for safe use as a PR title:
      1. Strip leading/trailing whitespace and control characters.
      2. Redact sequences that look like secrets or API tokens.
      3. Truncate to _PR_TITLE_MAX characters.
    """
    # Remove ASCII control characters (keep printable + space)
    title = re.sub(r"[\x00-\x1f\x7f]", " ", title).strip()
    # Redact token-like strings (20+ word-chars with no spaces)
    title = _SECRET_RE.sub(lambda m: "[REDACTED]" if _looks_like_secret(m.group()) else m.group(), title)
    # Truncate
    if len(title) > _PR_TITLE_MAX:
        title = title[:_PR_TITLE_MAX - 1] + "…"
    return title or "(untitled)"


def _looks_like_secret(s: str) -> bool:
    """
    Heuristic: a string looks like a secret if it is long (>=20 chars) and
    has high character-class diversity (letters + digits + symbols mixed
    together), making it unlikely to be ordinary prose.
    """
    if len(s) < 20:
        return False
    has_digit  = any(c.isdigit() for c in s)
    has_upper  = any(c.isupper() for c in s)
    has_lower  = any(c.islower() for c in s)
    has_symbol = any(c in "-_" for c in s)
    num_char_classes = sum([has_digit, has_upper, has_lower, has_symbol])
    # Plain lowercase/uppercase words are not secrets
    if num_char_classes < 3:
        return False
    return True


class CreateTaskRequest(BaseModel):
    title: str
    prompt: str
    repo_url: Optional[str] = None

    @field_validator("title")
    @classmethod
    def sanitize_title(cls, v: str) -> str:
        return _sanitize_title(v)

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _REPO_RE.match(v):
            raise ValueError("repo_url must be in owner/repo format (e.g. acme/my-repo)")
        return v

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        if len(v) > 8000:
            raise ValueError("prompt must be <= 8000 characters")
        return v


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Pings DB — returns degraded status if DB unreachable (F45)."""
    db_ok = await db.health_check()
    return {"status": "ok" if db_ok else "degraded", "db": db_ok}


@app.post("/tasks", status_code=201, dependencies=[Depends(require_auth)])
async def create_task(req: CreateTaskRequest, request: Request):
    task = await db.create_task(req.title, req.prompt, req.repo_url)
    task_id = task["id"]
    # Launch agent as a tracked asyncio Task (enables real cancellation)
    t = asyncio.create_task(run_task(task_id))
    _running[task_id] = t
    log.info("task_created", task_id=task_id, title=req.title)
    return task


@app.get("/tasks", dependencies=[Depends(require_auth)])
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Paginated task list (F29)."""
    tasks = await db.list_tasks(limit=limit, offset=offset)
    return {"tasks": tasks, "limit": limit, "offset": offset, "count": len(tasks)}


@app.get("/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def get_task(task_id: str):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    steps = await db.get_steps(task_id)
    logs  = await db.get_logs(task_id)
    return {"task": task, "steps": steps, "logs": logs}


@app.delete("/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def cancel_task(task_id: str):
    t = _running.get(task_id)
    if t and not t.done():
        t.cancel()
    await db.update_task(task_id, status="cancelled")
    log.info("task_cancelled", task_id=task_id)
    return {"status": "cancelled"}


@app.get("/tasks/{task_id}/stream", dependencies=[Depends(require_auth)])
async def stream_task(task_id: str, request: Request):
    """SSE stream with client-disconnect detection (F30)."""
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    bus = get_bus(task_id)

    async def event_generator():
        while True:
            # Check if client disconnected before each poll (F30)
            if await request.is_disconnected():
                log.info("sse_disconnect", task_id=task_id)
                break

            try:
                event = await asyncio.wait_for(bus.get(), timeout=25.0)
            except asyncio.TimeoutError:
                yield "event: heartbeat\ndata: {}\n\n"
                continue

            import json
            yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"

            # Terminal events — close stream
            if event["type"] in ("task_done", "task_failed", "task_cancelled"):
                break

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
