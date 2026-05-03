"""
FastAPI application — AI Coworker backend.

v2 fixes:
- F3/E1:  Optional Bearer token auth on all mutating endpoints
- F30/E4: SSE generator detects client disconnect (request.is_disconnected)
- F45:    /health pings the DB, not just returns OK
- F29:    GET /tasks supports ?limit=&offset= pagination
- F33:    repo_url validated as owner/repo format
- F51:    Removed duplicate uvicorn uvloop policy set (uvicorn handles it)
- F52:    structlog configured once at startup
"""
import asyncio
import hashlib
import hmac as _hmac
import json
import os as _os
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from typing import Deque, Dict, Optional

import structlog
import structlog.stdlib
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, field_validator

from backend import db
from backend.agents.registry import list_agents
from backend.agent_loop import _running, request_cancel, run_task
from backend.config import settings
from backend.events import subscribe, unsubscribe
from backend.notifier import notify_task_failure
from backend.recipes import list_recipes, normalize_workspace
from backend.supervisor import plan_task
from backend.operator_studio import artifact_index, handoff_summary, recipe_catalog, run_history


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
_reaper_task: Optional[asyncio.Task] = None
_backup_task: Optional[asyncio.Task] = None
_task_creation_lock = asyncio.Lock()
_task_create_rate_limiter: Dict[str, Deque[float]] = defaultdict(deque)
_task_create_rate_limit_lock = asyncio.Lock()


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _reaper_task, _backup_task
    log.info("startup", db_path=settings.db_path)
    if settings.environment == "production" and not settings.api_key:
        raise RuntimeError("API_KEY is required when ENV=production or APP_ENV=production")
    if not settings.api_key and not settings.insecure_local_auth:
        log.warning(
            "api_key_not_set_fail_closed",
            message="API_KEY is empty and INSECURE_LOCAL_AUTH is not set — "
                    "all protected endpoints will return 401. "
                    "Set INSECURE_LOCAL_AUTH=1 to allow unauthenticated access (localhost dev only).",
        )
    elif not settings.api_key and settings.insecure_local_auth:
        log.warning(
            "insecure_local_auth_enabled",
            message="INSECURE_LOCAL_AUTH=1 — all endpoints are unauthenticated. "
                    "NEVER use this in any networked deployment.",
        )
    await db.init_db()
    await _reconcile_interrupted_tasks()
    await _reap_zombie_tasks()
    _reaper_task = asyncio.create_task(_periodic_zombie_reaper())
    _backup_task = asyncio.create_task(_periodic_db_backup())
    try:
        yield
    finally:
        for task in (_reaper_task, _backup_task):
            if task:
                task.cancel()
        log.info("shutdown")


async def _periodic_zombie_reaper() -> None:
    while True:
        await asyncio.sleep(max(30, settings.zombie_reaper_interval_seconds))
        try:
            await _reap_zombie_tasks()
        except Exception as exc:
            log.warning("periodic_reaper_failed", error=str(exc))


async def _periodic_db_backup() -> None:
    while True:
        try:
            await db.backup_database()
        except Exception as exc:
            log.warning("db_backup_failed", error=str(exc))
        await asyncio.sleep(max(3600, settings.backup_interval_seconds))


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


def _recovery_note_for_interrupted_task(task: Dict) -> str:
    branch = task.get("branch")
    pr_url = task.get("pr_url")
    checkpoint = task.get("checkpoint_phase")
    if pr_url or checkpoint == "post_pr":
        url_str = f": {pr_url}" if pr_url else " (URL not saved; check GitHub)"
        return (
            f"Backend restarted while this task was running. A PR already exists{url_str}. "
            "Review that PR before retrying."
        )
    if branch or checkpoint == "post_branch":
        branch_str = branch if branch else "(branch name not saved; check GitHub)"
        return (
            f"Backend restarted before this task completed. Branch {branch_str!r} may contain partial work; "
            "review the branch before retrying."
        )
    return "Backend restarted before branch creation completed. No task branch is known; retry the task if needed."


async def _reconcile_interrupted_tasks() -> None:
    """Conservatively fail DB-running tasks that have no in-memory worker after startup."""
    running_tasks = await db.get_running_tasks()
    for task in running_tasks:
        task_id = task["id"]
        active = _running.get(task_id)
        if active and not active.done():
            continue
        repo = task.get("repo_url") or settings.github_default_repo
        note = _recovery_note_for_interrupted_task(task)
        reason = "task interrupted by backend restart"
        await db.update_task(
            task_id,
            status="failed",
            error=reason,
            recovery_note=note,
            reconciled_at=db._now(),
        )
        await db.add_log(task_id, "warning", f"{reason}: {note}")
        log.warning(
            "task_reconciled_after_restart",
            task_id=task_id,
            repo=repo,
            branch=task.get("branch"),
            pr_url=task.get("pr_url"),
            checkpoint_phase=task.get("checkpoint_phase"),
        )


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
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)


_TASK_RETRY_PATH_RE = re.compile(r"^/tasks/[^/]+/retry$")


def _is_task_mutation_request(request: Request) -> bool:
    return request.method == "POST" and (
        request.url.path == "/tasks" or bool(_TASK_RETRY_PATH_RE.fullmatch(request.url.path))
    )


@app.middleware("http")
async def enforce_task_request_size(request: Request, call_next):
    if _is_task_mutation_request(request) and settings.task_request_max_bytes > 0:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
            except ValueError:
                size = 0
            if size > settings.task_request_max_bytes:
                if settings.task_request_max_bytes >= 1024:
                    size_value = settings.task_request_max_bytes // 1024
                    unit = "KB"
                else:
                    size_value = settings.task_request_max_bytes
                    unit = "bytes"
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": f"Mission brief is too long (over {size_value} {unit}). Please shorten it and try again."},
                )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Auth dependency (F3/E1 / PR-B1)
# ---------------------------------------------------------------------------

_SSE_STREAM_PATH_RE = re.compile(r"^/tasks/[^/]+/stream$")


def _is_sse_stream_request(request: Request) -> bool:
    return request.method == "GET" and bool(_SSE_STREAM_PATH_RE.fullmatch(request.url.path))


# ---------------------------------------------------------------------------
# Short-lived SSE stream tokens (PR-B1)
# ---------------------------------------------------------------------------

def _generate_stream_token(task_id: str) -> str:
    """Generate a short-lived HMAC-SHA256 signed stream token scoped to *task_id*.

    Token format: ``{task_id}.{expiry_unix_int}.{hmac_hex}``
    Task IDs are UUIDs (hyphens only); expiry and HMAC hex contain no dots,
    so a 3-part split on ``"."`` is unambiguous.
    """
    expiry = int(time.time()) + settings.stream_token_ttl_seconds
    payload = f"{task_id}.{expiry}"
    sig = _hmac.new(settings.api_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify_stream_token(token: str, task_id: str) -> bool:
    """Return True iff *token* is a valid, unexpired stream token for *task_id*."""
    try:
        parts = token.split(".", 2)
        if len(parts) != 3:
            return False
        token_task_id, expiry_str, provided_sig = parts
        if token_task_id != task_id:
            return False
        expiry = int(expiry_str)
        if time.time() > expiry:
            return False
        payload = f"{task_id}.{expiry_str}"
        expected_sig = _hmac.new(
            settings.api_key.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return _hmac.compare_digest(expected_sig, provided_sig)
    except Exception:
        return False


def _fail_closed() -> None:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="API key required — provide a valid Bearer token or set your API key in settings.",
    )


async def require_auth(request: Request) -> None:
    """Validate master API key via Bearer header.

    Behaviour:
    - API_KEY set → require ``Authorization: Bearer <key>`` on every call.
    - API_KEY empty + INSECURE_LOCAL_AUTH=1 → allow all (localhost dev only).
    - API_KEY empty + INSECURE_LOCAL_AUTH unset → **fail-closed** (401).

    The ``?token=`` query-param path is intentionally NOT accepted here; use
    ``require_stream_auth`` on the SSE endpoint instead.
    """
    if not settings.api_key:
        if settings.insecure_local_auth:
            return
        _fail_closed()
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == settings.api_key:
        return
    _fail_closed()


async def require_stream_auth(request: Request) -> None:
    """Auth for the SSE stream endpoint.

    Accepted credentials (in priority order):
    1. ``Authorization: Bearer <master-api-key>`` header.
    2. ``?token=<stream-token>`` query param — short-lived token issued by
       ``POST /tasks/{task_id}/stream-token``.

    The master API key is *not* accepted as a ``?token=`` value; callers must
    obtain a stream token from the dedicated endpoint.
    """
    if not settings.api_key:
        if settings.insecure_local_auth:
            return
        _fail_closed()
    # 1. Master key via Authorization header (curl / server-side callers).
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == settings.api_key:
        return
    # 2. Short-lived stream token via ?token= (EventSource cannot send headers).
    token_param = request.query_params.get("token", "")
    if token_param and _is_sse_stream_request(request):
        task_id = request.path_params.get("task_id", "")
        if task_id and _verify_stream_token(token_param, task_id):
            return
    _fail_closed()


def _task_create_rate_limit_key(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if settings.api_key and auth.startswith("Bearer ") and auth[7:] == settings.api_key:
        return "api-key"
    if request.client and request.client.host:
        return f"ip:{request.client.host}"
    return "anonymous-dev"


async def _check_task_create_rate_limit(request: Request) -> None:
    if not settings.task_create_rate_limit_enabled:
        return
    limit = max(1, settings.task_create_rate_limit_count)
    window = max(1, settings.task_create_rate_limit_window_seconds)
    key = _task_create_rate_limit_key(request)
    now = time.monotonic()
    async with _task_create_rate_limit_lock:
        entries = _task_create_rate_limiter[key]
        while entries and now - entries[0] >= window:
            entries.popleft()
        if len(entries) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Tasks are being created too quickly — please wait {window} seconds and try again.",
            )
        entries.append(now)


def _reset_task_create_rate_limiter() -> None:
    _task_create_rate_limiter.clear()


def _prune_running_tasks() -> Dict[str, asyncio.Task]:
    active = {tid: task for tid, task in _running.items() if not task.done()}
    _running.clear()
    _running.update(active)
    return active


async def _active_task_count_for_workspace(workspace: str) -> int:
    active = _prune_running_tasks()
    count = 0
    for task_id in active:
        task = await db.get_task(task_id)
        if task and task.get("workspace") == workspace:
            count += 1
    return count


async def _validate_task_workspace_refs(req: CreateTaskRequest) -> None:
    if req.project_id:
        proj = await db.get_project(req.project_id, workspace=req.workspace)
        if not proj:
            raise HTTPException(status_code=422, detail="project_id not found for this workspace")
    if req.client_id:
        cl = await db.get_client(req.client_id, workspace=req.workspace)
        if not cl:
            raise HTTPException(status_code=422, detail="client_id not found for this workspace")


async def _create_and_start_task(req: CreateTaskRequest) -> Dict:
    await _validate_task_workspace_refs(req)
    active_count = await _active_task_count_for_workspace(req.workspace)
    if active_count >= settings.max_concurrent_tasks:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A task is already active in this workspace ({active_count}/{settings.max_concurrent_tasks}). Wait for it to finish, then try again.",
        )
    task = await db.create_task(
        req.title,
        req.prompt,
        req.repo_url,
        workspace=req.workspace,
        project_id=req.project_id,
        client_id=req.client_id,
    )
    task_id = task["id"]
    t = asyncio.create_task(run_task(task_id))
    _running[task_id] = t
    return task


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
    workspace: str = "personal"
    project_id: Optional[str] = None
    client_id: Optional[str] = None

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

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        try:
            return normalize_workspace(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class TaskPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notes: Optional[str] = None
    pinned: Optional[bool] = None
    public: Optional[bool] = None
    project_id: Optional[str] = None


class TaskEstimateRequest(BaseModel):
    prompt: str

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        if len(v) > 8000:
            raise ValueError("prompt must be <= 8000 characters")
        return v


class ProjectCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    workspace: str = "personal"

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        try:
            return normalize_workspace(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class RevenueCreateRequest(BaseModel):
    amount_usd: float
    source: str
    note: Optional[str] = None
    project_id: Optional[str] = None
    workspace: str = "personal"

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        try:
            return normalize_workspace(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ClientCreateRequest(BaseModel):
    name: str
    workspace: str = "work"
    github_org: Optional[str] = None
    budget_usd: Optional[float] = None

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        try:
            return normalize_workspace(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ClientPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = None
    github_org: Optional[str] = None
    budget_usd: Optional[float] = None


class SupervisorPlanRequest(BaseModel):
    prompt: str
    workspace: str = "personal"

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        try:
            return normalize_workspace(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ArtifactCreateRequest(BaseModel):
    artifact_type: str
    title: str
    content: Dict
    task_id: Optional[str] = None
    workspace: str = "personal"
    created_by: Optional[str] = None

    @field_validator("workspace")
    @classmethod
    def validate_workspace(cls, v: str) -> str:
        try:
            return normalize_workspace(v)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


class ArtifactVersionRequest(BaseModel):
    content: Dict
    created_by: Optional[str] = None
    change_note: Optional[str] = None


def _workspace_query(workspace: str = Query(default="personal")) -> str:
    try:
        return db.normalize_workspace(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# PR-C1: safe size bound per step field (characters) — prevents huge payloads.
_STEP_MAX_FIELD_CHARS = 50_000


def _bound_step(step: Dict) -> Dict:
    """Apply safe size bounds to step fields (PR-C1). Truncates oversized fields and
    records which fields were truncated in the ``_truncated_fields`` key."""
    out = dict(step)
    truncated = []
    for field in ("tool_input", "tool_output", "reasoning"):
        val = out.get(field)
        if val and len(val) > _STEP_MAX_FIELD_CHARS:
            out[field] = val[:_STEP_MAX_FIELD_CHARS]
            truncated.append(field)
    if truncated:
        out["_truncated_fields"] = truncated
    return out


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """Pings DB — returns degraded status if DB unreachable (F45)."""
    db_detail = await db.health_detail()
    return {
        "status": "ok" if db_detail.get("ok") else "degraded",
        "db": bool(db_detail.get("ok")),
        "db_detail": db_detail,
        "max_task_usd": settings.max_task_usd,
        "max_concurrent_tasks": settings.max_concurrent_tasks,
        "model": settings.model,
        "watchdog_model": settings.watchdog_model,
    }


@app.get("/agents", dependencies=[Depends(require_auth)])
async def agents(workspace: Optional[str] = None):
    try:
        normalized = normalize_workspace(workspace) if workspace else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"agents": list_agents(normalized)}


@app.get("/recipes", dependencies=[Depends(require_auth)])
async def recipes(workspace: Optional[str] = None):
    try:
        return {"recipes": list_recipes(workspace)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/supervisor/plan", dependencies=[Depends(require_auth)])
async def supervisor_plan(req: SupervisorPlanRequest):
    return plan_task(req.prompt, req.workspace)


@app.post("/tasks", status_code=201, dependencies=[Depends(require_auth)])
async def create_task(req: CreateTaskRequest, request: Request):
    # Daily budget cap (A8)
    if settings.daily_max_usd > 0:
        daily_spend = await db.get_daily_spend(workspace=req.workspace)
        if daily_spend >= settings.daily_max_usd:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Daily API budget (${settings.daily_max_usd:.2f}) reached — "
                    "tasks paused until tomorrow."
                ),
            )

    await _check_task_create_rate_limit(request)
    async with _task_creation_lock:
        task = await _create_and_start_task(req)
    log.info("task_created", task_id=task["id"], title=req.title)
    return task


@app.get("/summary", dependencies=[Depends(require_auth)])
async def summary(workspace: str = Depends(_workspace_query)):
    """Return task counts and USD spend for today and the last 7 days (A9)."""
    return await db.get_summary(workspace=workspace)


@app.get("/metrics", dependencies=[Depends(require_auth)])
async def metrics(
    window: int = Query(default=24, ge=1, le=168, description="Lookback window in hours (1-168)"),
):
    """Return aggregated reliability and cost metrics for the given time window (A1).

    Supports any window from 1h to 168h (7 days).
    Returns success_rate, failure_rate, latency stats, failure_category distribution,
    and cost summary derived from explicit schema fields.
    """
    return await db.get_metrics(window_hours=window)


@app.post("/artifacts", status_code=201, dependencies=[Depends(require_auth)])
async def create_artifact(req: ArtifactCreateRequest):
    if req.task_id:
        task = await db.get_task(req.task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
    try:
        return await db.create_artifact(
            artifact_type=req.artifact_type,
            title=req.title,
            content_json=json.dumps(req.content, sort_keys=True),
            task_id=req.task_id,
            workspace=req.workspace,
            created_by=req.created_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/artifacts", dependencies=[Depends(require_auth)])
async def artifacts(task_id: Optional[str] = None, workspace: Optional[str] = None):
    try:
        return {"artifacts": await db.list_artifacts(task_id=task_id, workspace=workspace)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _assert_artifact_workspace(artifact: Dict, workspace: str) -> None:
    try:
        normalized = normalize_workspace(workspace)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if artifact.get("workspace") != normalized:
        raise HTTPException(status_code=404, detail="Artifact not found")


@app.get("/artifacts/{artifact_id}", dependencies=[Depends(require_auth)])
async def get_artifact(artifact_id: str, workspace: str = Query(...)):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    _assert_artifact_workspace(artifact, workspace)
    return artifact


@app.post("/artifacts/{artifact_id}/versions", dependencies=[Depends(require_auth)])
async def add_artifact_version(artifact_id: str, req: ArtifactVersionRequest, workspace: str = Query(...)):
    artifact = await db.get_artifact(artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    _assert_artifact_workspace(artifact, workspace)
    try:
        return await db.add_artifact_version(
            artifact_id,
            json.dumps(req.content, sort_keys=True),
            created_by=req.created_by,
            change_note=req.change_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/operator/status", dependencies=[Depends(require_auth)])
async def operator_status():
    """Private operator status surface for local supervision and recovery."""
    db_detail = await db.health_detail()
    spend = await db.get_summary()
    return {
        "app": {
            "name": "AI Coworker",
            "version": app.version,
            "environment": settings.environment,
            "auth_required": bool(settings.api_key),
            "model": settings.model,
            "watchdog_model": settings.watchdog_model,
        },
        "db": db_detail,
        "tasks": {
            "counts_by_status": await db.get_task_counts_by_status(),
            "running": await db.get_running_task_summaries(),
            "recent_failed": await db.get_recent_failed_task_summaries(limit=10),
        },
        "spend": {
            "today_usd": spend["total_usd_today"],
            "week_usd": spend["total_usd_this_week"],
            "summary": spend,
        },
        "backup": await db.backup_status(),
        "guardrails": {
            "max_steps": settings.max_steps,
            "step_timeout_seconds": settings.step_timeout_seconds,
            "task_timeout_seconds": settings.task_timeout_seconds,
            "max_concurrent_tasks": settings.max_concurrent_tasks,
            "task_create_rate_limit_enabled": settings.task_create_rate_limit_enabled,
            "task_create_rate_limit_count": settings.task_create_rate_limit_count,
            "task_create_rate_limit_window_seconds": settings.task_create_rate_limit_window_seconds,
            "task_request_max_bytes": settings.task_request_max_bytes,
            "max_task_usd": settings.max_task_usd,
            "daily_max_usd": settings.daily_max_usd,
            "zombie_reaper_interval_seconds": settings.zombie_reaper_interval_seconds,
        },
    }


@app.post("/operator/backup", dependencies=[Depends(require_auth)])
async def operator_backup():
    """Trigger a local SQLite backup for the private operator."""
    backup_path = await db.backup_database()
    return {
        "created": bool(backup_path),
        "backup": await db.backup_status(),
    }


@app.get("/tasks", dependencies=[Depends(require_auth)])
async def list_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    workspace: str = Depends(_workspace_query),
):
    """Paginated task list (F29)."""
    tasks = await db.list_tasks(limit=limit, offset=offset, workspace=workspace)
    return {"tasks": tasks, "workspace": workspace, "limit": limit, "offset": offset, "count": len(tasks), "max_task_usd": settings.max_task_usd}


@app.get("/operator/recipes", dependencies=[Depends(require_auth)])
async def operator_recipes(category: str = Query(default="")):
    """Built-in recipe previews for the operator studio."""
    return recipe_catalog(category=category)


@app.get("/operator/run-history", dependencies=[Depends(require_auth)])
async def operator_run_history(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    workspace: str = Depends(_workspace_query),
):
    """Recent task runs summarized for fast operator review."""
    tasks = await db.list_tasks(limit=limit, offset=offset, workspace=workspace)
    data = run_history(tasks)
    data.update({"limit": limit, "offset": offset})
    return data


@app.get("/operator/artifacts", dependencies=[Depends(require_auth)])
async def operator_artifacts(
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    workspace: str = Depends(_workspace_query),
):
    """Derived artifact index backed by task runs until artifact tables land."""
    tasks = await db.list_tasks(limit=limit, offset=offset, workspace=workspace)
    data = artifact_index(tasks)
    data.update({"limit": limit, "offset": offset})
    return data


@app.get("/operator/handoff/{task_id}", dependencies=[Depends(require_auth)])
async def operator_handoff(task_id: str, workspace: str = Depends(_workspace_query)):
    """Traceable handoff summary for a single task."""
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    steps = await db.get_steps(task_id)
    logs = await db.get_logs(task_id)
    return handoff_summary(task, steps, logs)


@app.get("/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def get_task(task_id: str, workspace: str = Depends(_workspace_query)):
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    steps = await db.get_steps(task_id)
    logs  = await db.get_logs(task_id)
    return {"task": task, "steps": steps, "logs": logs}


@app.get("/tasks/{task_id}/steps/{step_id}", dependencies=[Depends(require_auth)])
async def get_step_detail(task_id: str, step_id: str, workspace: str = Depends(_workspace_query)):
    """Return full step detail with safe size bounds (PR-C1).

    Fields larger than _STEP_MAX_FIELD_CHARS are truncated; the response
    includes a ``_truncated_fields`` list when truncation occurred so the
    caller can detect partial data and decide whether to paginate further.
    Redaction applied at write-time is preserved.
    """
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    step = await db.get_step(task_id, step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    return _bound_step(step)


@app.delete("/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def cancel_task(task_id: str, workspace: str = Depends(_workspace_query)):
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    running_task = _running.get(task_id)
    if running_task and not running_task.done():
        running_task.cancel()
    request_cancel(task_id)
    await db.update_task(task_id, status="cancelled")
    log.info("task_cancelled", task_id=task_id)
    return {"status": "cancelled"}


@app.post("/tasks/{task_id}/retry", status_code=201, dependencies=[Depends(require_auth)])
async def retry_task(task_id: str, request: Request, workspace: str = Depends(_workspace_query)):
    """Retry a failed/cancelled task. Retries share the task creation rate limit."""
    original = await db.get_task(task_id, workspace=workspace)
    if not original:
        raise HTTPException(status_code=404, detail="Task not found")
    if original["status"] not in {"failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Only failed or cancelled tasks can be retried")
    req = CreateTaskRequest(
        title=f"Retry: {original['title']}",
        prompt=original["prompt"],
        repo_url=original.get("repo_url"),
        workspace=original["workspace"],
        project_id=original.get("project_id"),
        client_id=original.get("client_id"),
    )
    await _check_task_create_rate_limit(request)
    async with _task_creation_lock:
        task = await _create_and_start_task(req)
    log.info("task_retried", original_task_id=task_id, task_id=task["id"])
    return task


@app.post("/tasks/{task_id}/stream-token", dependencies=[Depends(require_auth)])
async def create_stream_token(task_id: str, workspace: str = Depends(_workspace_query)):
    """Issue a short-lived SSE stream token scoped to *task_id* (PR-B1).

    The token is signed with HMAC-SHA256 and expires after
    ``settings.stream_token_ttl_seconds`` seconds.  Pass it to
    ``GET /tasks/{task_id}/stream?token=<token>`` from an EventSource that
    cannot send custom headers.

    Requires master API key via ``Authorization: Bearer`` header.
    """
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    token = _generate_stream_token(task_id)
    return {"token": token, "task_id": task_id, "expires_in": settings.stream_token_ttl_seconds}


@app.get("/tasks/{task_id}/stream", dependencies=[Depends(require_stream_auth)])
async def stream_task(task_id: str, request: Request, workspace: str = Depends(_workspace_query)):
    """SSE stream with client-disconnect detection (F30)."""
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    queue = subscribe(task_id)

    async def event_generator():
        try:
            while True:
                # Check if client disconnected before each poll (F30)
                if await request.is_disconnected():
                    log.info("sse_disconnect", task_id=task_id)
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield "event: heartbeat\ndata: {}\n\n"
                    continue

                import json
                seq_line = f"id: {event['seq']}\n" if "seq" in event else ""
                yield f"{seq_line}event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"

                # Terminal events — close stream
                if event["type"] in ("task_done", "task_failed", "task_cancelled"):
                    break
        finally:
            unsubscribe(task_id, queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.patch("/tasks/{task_id}", dependencies=[Depends(require_auth)])
async def patch_task(task_id: str, req: TaskPatchRequest, workspace: str = Depends(_workspace_query)):
    task = await db.get_task(task_id, workspace=workspace)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    payload = req.model_dump(exclude_unset=True)
    if "project_id" in payload and payload["project_id"]:
        proj = await db.get_project(payload["project_id"], workspace=workspace)
        if not proj:
            raise HTTPException(status_code=422, detail="project_id not found for this workspace")
    if "pinned" in payload:
        payload["pinned"] = 1 if payload["pinned"] else 0
    if "public" in payload:
        payload["public"] = 1 if payload["public"] else 0
    await db.update_task(task_id, **payload)
    updated = await db.get_task(task_id, workspace=workspace)
    return {"task": updated}


@app.post("/tasks/estimate", dependencies=[Depends(require_auth)])
async def estimate_task(req: TaskEstimateRequest):
    words = len(req.prompt.split())
    estimated_minutes = max(1, int(words * 0.4))
    estimated_cost_usd = round(estimated_minutes * 0.05, 2)
    return {
        "word_count": words,
        "estimated_minutes": estimated_minutes,
        "estimated_cost_usd": estimated_cost_usd,
    }


@app.get("/tasks/{task_id}/public")
async def get_task_public(task_id: str):
    data = await db.get_public_task(task_id)
    if not data:
        raise HTTPException(status_code=404, detail="Not found")
    return data


@app.get("/tasks/public/portfolio")
async def public_task_portfolio(limit: int = Query(default=50, ge=1, le=200)):
    return {"tasks": await db.list_public_tasks_done(limit=limit)}


@app.get("/analytics", dependencies=[Depends(require_auth)])
async def analytics(workspace: str = Query(default="all")):
    return await db.get_analytics(workspace=workspace)


@app.get("/activity", dependencies=[Depends(require_auth)])
async def activity_feed(limit: int = Query(default=20, ge=1, le=100)):
    return {"events": await db.list_recent_activity(limit=limit)}


@app.post("/projects", status_code=201, dependencies=[Depends(require_auth)])
async def create_project_api(req: ProjectCreateRequest):
    return await db.create_project(req.name, workspace=req.workspace, description=req.description)


@app.get("/projects", dependencies=[Depends(require_auth)])
async def list_projects_api(workspace: str = Depends(_workspace_query)):
    return {"projects": await db.list_projects(workspace=workspace)}


@app.post("/revenue", status_code=201, dependencies=[Depends(require_auth)])
async def create_revenue(req: RevenueCreateRequest):
    if req.project_id:
        proj = await db.get_project(req.project_id, workspace=req.workspace)
        if not proj:
            raise HTTPException(status_code=422, detail="project_id not found for this workspace")
    return await db.create_revenue_log(
        workspace=req.workspace,
        amount_usd=req.amount_usd,
        source=req.source,
        note=req.note,
        project_id=req.project_id,
    )


@app.get("/revenue", dependencies=[Depends(require_auth)])
async def list_revenue(workspace: str = Depends(_workspace_query)):
    return {
        "entries": await db.list_revenue_logs(workspace=workspace),
        "summary": await db.revenue_totals_by_source(workspace=workspace),
    }


@app.post("/clients", status_code=201, dependencies=[Depends(require_auth)])
async def create_client_api(req: ClientCreateRequest):
    return await db.create_client(
        req.name, workspace=req.workspace, github_org=req.github_org, budget_usd=req.budget_usd
    )


@app.get("/clients", dependencies=[Depends(require_auth)])
async def list_clients_api(workspace: str = Depends(_workspace_query)):
    return {"clients": await db.list_clients(workspace=workspace)}


@app.patch("/clients/{client_id}", dependencies=[Depends(require_auth)])
async def patch_client_api(client_id: str, req: ClientPatchRequest, workspace: str = Depends(_workspace_query)):
    payload = req.model_dump(exclude_unset=True)
    if not payload:
        row = await db.get_client(client_id, workspace=workspace)
        if not row:
            raise HTTPException(status_code=404, detail="Client not found")
        return row
    updated = await db.update_client(client_id, workspace, **payload)
    if not updated:
        raise HTTPException(status_code=404, detail="Client not found")
    return updated


@app.delete("/clients/{client_id}", dependencies=[Depends(require_auth)])
async def delete_client_api(client_id: str, workspace: str = Depends(_workspace_query)):
    ok = await db.delete_client(client_id, workspace)
    if not ok:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"deleted": True}
