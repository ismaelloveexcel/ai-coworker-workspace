"""
Database layer — SQLite WAL mode via aiosqlite.

v2: F18/E10 schema versioning + migrations, F19 column allowlist,
    F17 proper async context manager, F29 paginated list_tasks,
    F35 correct Optional typing, F45 health_check().
    Added usd_spent / heartbeat_at columns (operator survival kit).
"""
import os
import asyncio
import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiosqlite

from backend.config import settings

# ---------------------------------------------------------------------------
# Shared connection for :memory: mode (each new connect() creates an empty DB)
# ---------------------------------------------------------------------------
_memory_conn: Optional[aiosqlite.Connection] = None
_memory_conn_lock: asyncio.Lock | None = None


def _get_memory_lock() -> asyncio.Lock:
    """Return (lazily creating) the per-event-loop lock for _memory_conn init."""
    global _memory_conn_lock
    if _memory_conn_lock is None:
        _memory_conn_lock = asyncio.Lock()
    return _memory_conn_lock


async def _get_memory_conn() -> aiosqlite.Connection:
    global _memory_conn
    async with _get_memory_lock():
        if _memory_conn is None:
            _memory_conn = await aiosqlite.connect(":memory:", timeout=30)
            _memory_conn.row_factory = aiosqlite.Row
            await _memory_conn.execute("PRAGMA journal_mode=WAL")
            await _memory_conn.execute("PRAGMA synchronous=NORMAL")
            await _memory_conn.execute("PRAGMA foreign_keys=ON")
    return _memory_conn


def _redact_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    from backend.tool_adapters import _redact
    return _redact(value)

# ---------------------------------------------------------------------------
# Schema + migrations
# ---------------------------------------------------------------------------

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS tasks (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    prompt     TEXT NOT NULL,
    repo_url   TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
    pr_url     TEXT,
    error      TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS steps (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_num    INTEGER NOT NULL,
    tool_name   TEXT,
    tool_input  TEXT,
    tool_output TEXT,
    status      TEXT NOT NULL DEFAULT 'running',
    reasoning   TEXT,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS logs (
    id         TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    level      TEXT NOT NULL DEFAULT 'info',
    message    TEXT NOT NULL,
    meta       TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_steps_task   ON steps (task_id, step_num);
CREATE INDEX IF NOT EXISTS idx_logs_task    ON logs  (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status, created_at);
"""

_MIGRATIONS: List[str] = [_SCHEMA_V1]
# Append future migrations here, e.g.:
# _MIGRATIONS.append("ALTER TABLE tasks ADD COLUMN cost_usd_cents INTEGER DEFAULT 0;")

_SCHEMA_V2 = """
ALTER TABLE tasks ADD COLUMN usd_spent REAL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN heartbeat_at TEXT NULL;
"""
_MIGRATIONS.append(_SCHEMA_V2)

_SCHEMA_V3 = """
ALTER TABLE tasks ADD COLUMN branch TEXT NULL;
ALTER TABLE tasks ADD COLUMN current_step INTEGER DEFAULT 0;
ALTER TABLE tasks ADD COLUMN last_action TEXT NULL;
ALTER TABLE tasks ADD COLUMN last_tool TEXT NULL;
ALTER TABLE tasks ADD COLUMN recovery_note TEXT NULL;
ALTER TABLE tasks ADD COLUMN reconciled_at TEXT NULL;
"""
_MIGRATIONS.append(_SCHEMA_V3)

_SCHEMA_V4 = """
ALTER TABLE tasks ADD COLUMN workspace TEXT NOT NULL DEFAULT 'personal';
CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    workspace       TEXT NOT NULL DEFAULT 'personal',
    artifact_type   TEXT NOT NULL,
    title           TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    created_by      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS artifact_versions (
    id           TEXT PRIMARY KEY,
    artifact_id  TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    version_num  INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    created_by   TEXT,
    change_note  TEXT,
    created_at   TEXT DEFAULT (datetime('now')),
    UNIQUE(artifact_id, version_num)
);
CREATE TABLE IF NOT EXISTS artifact_links (
    id                 TEXT PRIMARY KEY,
    artifact_id        TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    linked_artifact_id TEXT NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    relation           TEXT NOT NULL,
    created_at         TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_artifacts_task ON artifacts (task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_workspace ON artifacts (workspace, created_at);
CREATE INDEX IF NOT EXISTS idx_artifact_versions_artifact ON artifact_versions (artifact_id, version_num);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace_status ON tasks (workspace, status, created_at);
"""
_MIGRATIONS.append(_SCHEMA_V4)

_SCHEMA_V5 = """
ALTER TABLE tasks ADD COLUMN checkpoint_phase TEXT NULL;
ALTER TABLE tasks ADD COLUMN started_at TEXT NULL;
ALTER TABLE tasks ADD COLUMN ended_at TEXT NULL;
ALTER TABLE tasks ADD COLUMN failure_category TEXT NULL;
"""
_MIGRATIONS.append(_SCHEMA_V5)

_SCHEMA_V6 = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT 'personal',
    name TEXT NOT NULL,
    description TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects (workspace, created_at);
CREATE TABLE IF NOT EXISTS revenue_log (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT 'personal',
    project_id TEXT,
    amount_usd REAL NOT NULL,
    source TEXT NOT NULL,
    note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_revenue_workspace ON revenue_log (workspace, created_at);
CREATE INDEX IF NOT EXISTS idx_revenue_project ON revenue_log (project_id);
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY,
    workspace TEXT NOT NULL DEFAULT 'work',
    name TEXT NOT NULL,
    github_org TEXT,
    budget_usd REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clients_workspace ON clients (workspace, created_at);
ALTER TABLE tasks ADD COLUMN project_id TEXT;
ALTER TABLE tasks ADD COLUMN client_id TEXT;
ALTER TABLE tasks ADD COLUMN public INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN notes TEXT;
ALTER TABLE tasks ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
"""
_MIGRATIONS.append(_SCHEMA_V6)

# Valid failure categories — explicit set prevents fragile free-text parsing (A1)
VALID_FAILURE_CATEGORIES = frozenset({
    "branch_creation_failed",
    "budget_exceeded",
    "timeout",
    "malformed_output",
    "no_changes",
    "validation_failed",
    "pr_creation_failed",
    "agent_error",
})

# ---------------------------------------------------------------------------
# Column allowlists (F19)
# ---------------------------------------------------------------------------

_TASK_UPDATABLE = frozenset({
    "status", "pr_url", "error", "updated_at", "heartbeat_at", "usd_spent",
    "branch", "current_step", "last_action", "last_tool", "recovery_note", "reconciled_at",
    "workspace", "checkpoint_phase", "started_at", "ended_at", "failure_category",
    "project_id", "client_id", "public", "notes", "pinned",
})
_STEP_UPDATABLE = frozenset({"status", "tool_name", "tool_input", "tool_output", "reasoning", "updated_at"})
_ARTIFACT_TYPES = frozenset({
    "app", "document", "slide_deck", "spreadsheet", "research_brief", "campaign_pack",
    "workflow", "code_diff", "dashboard", "design_mockup", "automation_blueprint", "knowledge_base",
})
VALID_WORKSPACES = frozenset({"personal", "work"})


def normalize_workspace(workspace: Optional[str] = None) -> str:
    value = (workspace or "personal").strip().lower()
    if value not in VALID_WORKSPACES:
        raise ValueError(f"workspace must be one of: {', '.join(sorted(VALID_WORKSPACES))}")
    return value


def _validate_artifact_type(artifact_type: str) -> str:
    value = (artifact_type or "").strip().lower()
    if value not in _ARTIFACT_TYPES:
        raise ValueError(f"artifact_type must be one of: {', '.join(sorted(_ARTIFACT_TYPES))}")
    return value


def _safe_cols(allowed: frozenset, kwargs: dict) -> dict:
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"Attempt to update disallowed columns: {bad}")
    return kwargs


# ---------------------------------------------------------------------------
# Connection context manager (F17)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _get_db():
    """Single-use WAL connection; commits on clean exit, rolls back on error.
    For :memory: databases a shared connection is reused to preserve state."""
    if settings.db_path == ":memory:":
        db = await _get_memory_conn()
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        return
    os.makedirs(os.path.dirname(os.path.abspath(settings.db_path)), exist_ok=True)
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=30000")
        try:
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise


# ---------------------------------------------------------------------------
# Init / migrations
# ---------------------------------------------------------------------------

async def init_db() -> None:
    if settings.db_path == ":memory:":
        db = await _get_memory_conn()
        await _apply_migrations(db)
        await db.commit()
        return
    os.makedirs(os.path.dirname(os.path.abspath(settings.db_path)), exist_ok=True)
    async with aiosqlite.connect(settings.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=30000")
        await _apply_migrations(db)
        await db.commit()


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
    row = await (await db.execute("SELECT COALESCE(MAX(version),0) FROM schema_version")).fetchone()
    current = row[0]
    for idx, sql in enumerate(_MIGRATIONS, 1):
        if idx > current:
            stmts = [s.strip() for s in sql.split(";") if s.strip()]
            for stmt in stmts:
                if stmt.upper().startswith("ALTER TABLE"):
                    try:
                        await db.execute(stmt)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column" not in str(exc).lower():
                            raise
                else:
                    await db.execute(stmt)
            await db.execute("INSERT INTO schema_version (version) VALUES (?)", (idx,))


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()



async def create_task(
    title: str,
    prompt: str,
    repo_url: Optional[str] = None,
    workspace: str = "personal",
    project_id: Optional[str] = None,
    client_id: Optional[str] = None,
) -> Dict:
    task_id = str(uuid.uuid4())
    safe_title = _redact_text(title) or title
    safe_prompt = _redact_text(prompt) or prompt
    workspace = normalize_workspace(workspace)
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO tasks (id, title, prompt, repo_url, status, workspace, project_id, client_id) "
            "VALUES (?,?,?,?, 'pending', ?, ?, ?)",
            (task_id, safe_title, safe_prompt, repo_url, workspace, project_id, client_id),
        )
    return {
        "id": task_id,
        "title": safe_title,
        "prompt": safe_prompt,
        "repo_url": repo_url,
        "workspace": workspace,
        "project_id": project_id,
        "client_id": client_id,
        "status": "pending",
        "created_at": _now(),
    }


async def get_task(task_id: str, workspace: Optional[str] = None) -> Optional[Dict]:
    async with _get_db() as db:
        if workspace is None:
            row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
        else:
            normalized = normalize_workspace(workspace)
            row = await (await db.execute("SELECT * FROM tasks WHERE id=? AND workspace=?", (task_id, normalized))).fetchone()
    return dict(row) if row else None


async def list_tasks(limit: int = 50, offset: int = 0, workspace: str = "personal") -> List[Dict]:
    """Paginated — F29. Pinned tasks sort first."""
    workspace = normalize_workspace(workspace)
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM tasks WHERE workspace=? "
            "ORDER BY COALESCE(pinned,0) DESC, created_at DESC LIMIT ? OFFSET ?",
            (workspace, limit, offset),
        )).fetchall()
    return [dict(r) for r in rows]


async def update_task(task_id: str, **kwargs) -> None:
    kwargs = _safe_cols(_TASK_UPDATABLE, kwargs)
    for key in ("error", "recovery_note", "notes"):
        if key in kwargs:
            kwargs[key] = _redact_text(kwargs[key])
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    async with _get_db() as db:
        await db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*kwargs.values(), task_id))


async def delete_task(task_id: str) -> None:
    async with _get_db() as db:
        await db.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ---------------------------------------------------------------------------
# Step CRUD
# ---------------------------------------------------------------------------

async def create_step(task_id: str, step_num: int,
                      tool_name: Optional[str] = None,
                      tool_input: Optional[str] = None) -> str:
    step_id = str(uuid.uuid4())
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO steps (id,task_id,step_num,tool_name,tool_input,status) VALUES (?,?,?,?,?,'running')",
            (step_id, task_id, step_num, tool_name, tool_input),
        )
    return step_id


async def update_step(step_id: str, **kwargs) -> None:
    kwargs = _safe_cols(_STEP_UPDATABLE, kwargs)
    for key in ("tool_input", "tool_output", "reasoning"):
        if key in kwargs:
            kwargs[key] = _redact_text(kwargs[key])
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    async with _get_db() as db:
        await db.execute(f"UPDATE steps SET {cols} WHERE id=?", (*kwargs.values(), step_id))


async def get_steps(task_id: str) -> List[Dict]:
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY step_num ASC", (task_id,)
        )).fetchall()
    return [dict(r) for r in rows]


async def get_step(task_id: str, step_id: str) -> Optional[Dict]:
    """Fetch a single step scoped to task_id (prevents cross-task access)."""
    async with _get_db() as db:
        row = await (await db.execute(
            "SELECT * FROM steps WHERE id=? AND task_id=?", (step_id, task_id)
        )).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

async def add_log(task_id: str, level: str, message: str,
                  meta: Optional[str] = None) -> None:
    log_id = str(uuid.uuid4())
    message = _redact_text(message) or ""
    meta = _redact_text(meta)
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO logs (id,task_id,level,message,meta) VALUES (?,?,?,?,?)",
            (log_id, task_id, level, message, meta),
        )


async def get_logs(task_id: str, limit: int = 200) -> List[Dict]:
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM logs WHERE task_id=? ORDER BY created_at ASC LIMIT ?", (task_id, limit)
        )).fetchall()
    return [dict(r) for r in rows]


async def touch_heartbeat(task_id: str) -> None:
    """Update heartbeat_at to now; called once per agent step."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with _get_db() as db:
        await db.execute(
            "UPDATE tasks SET heartbeat_at=? WHERE id=?",
            (now, task_id),
        )


async def add_usd_spent(task_id: str, amount: float) -> float:
    """Atomically increment usd_spent and return the new cumulative total.

    Raises ValueError if no task row is found, which prevents silently
    misreporting spend when a bad task_id is passed.
    """
    async with _get_db() as db:
        row = await (await db.execute(
            "UPDATE tasks SET usd_spent = usd_spent + ? WHERE id=? RETURNING usd_spent",
            (amount, task_id),
        )).fetchone()
    if row is None:
        raise ValueError(f"Task not found for usd_spent update: {task_id}")
    return row["usd_spent"]


async def get_zombie_tasks(stale_minutes: int = 10) -> List[Dict]:
    """Return running tasks whose heartbeat (or created_at) is older than stale_minutes.

    Uses YYYY-MM-DD HH:MM:SS format for the cutoff so it compares correctly
    against both SQLite's datetime('now') default (same format) and Python
    strftime-stored heartbeat_at values.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    async with _get_db() as db:
        rows = await (await db.execute(
            """SELECT * FROM tasks
               WHERE status='running'
               AND created_at < ?
               AND (heartbeat_at IS NULL OR heartbeat_at < ?)""",
            (cutoff, cutoff),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_running_tasks() -> List[Dict]:
    """Return all tasks currently marked running in the DB."""
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM tasks WHERE status='running' ORDER BY created_at ASC"
        )).fetchall()
    return [dict(r) for r in rows]


_OPERATOR_TASK_FIELDS = (
    "id", "title", "repo_url", "status", "branch", "current_step", "last_action",
    "last_tool", "heartbeat_at", "recovery_note", "pr_url", "error", "created_at", "updated_at",
)


def _operator_task_row(row: aiosqlite.Row) -> Dict:
    task = {field: row[field] for field in _OPERATOR_TASK_FIELDS if field in row.keys()}
    task["repo"] = task.pop("repo_url", None)
    for field in ("error", "recovery_note"):
        if task.get(field):
            task[field] = _redact_text(task[field])
    return task


async def get_task_counts_by_status() -> Dict[str, int]:
    """Return task counts grouped by status for operator dashboards."""
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT status, COUNT(*) as count FROM tasks GROUP BY status ORDER BY status ASC"
        )).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


async def get_running_task_summaries() -> List[Dict]:
    """Return allowlisted operational fields for running tasks."""
    async with _get_db() as db:
        rows = await (await db.execute(
            """SELECT id, title, repo_url, status, branch, current_step, last_action,
                      last_tool, heartbeat_at, recovery_note, pr_url, error, created_at, updated_at
               FROM tasks
               WHERE status='running'
               ORDER BY created_at ASC"""
        )).fetchall()
    return [_operator_task_row(row) for row in rows]


async def get_recent_failed_task_summaries(limit: int = 10) -> List[Dict]:
    """Return allowlisted operational fields for recent failed/interrupted tasks."""
    async with _get_db() as db:
        rows = await (await db.execute(
            """SELECT id, title, repo_url, status, branch, current_step, last_action,
                      last_tool, heartbeat_at, recovery_note, pr_url, error, created_at, updated_at
               FROM tasks
               WHERE status IN ('failed', 'cancelled')
               ORDER BY updated_at DESC
               LIMIT ?""",
            (limit,),
        )).fetchall()
    return [_operator_task_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

async def create_artifact(
    *,
    artifact_type: str,
    title: str,
    content_json: str,
    task_id: Optional[str] = None,
    workspace: str = "personal",
    created_by: Optional[str] = None,
) -> Dict:
    artifact_id = str(uuid.uuid4())
    version_id = str(uuid.uuid4())
    safe_workspace = normalize_workspace(workspace)
    safe_type = _validate_artifact_type(artifact_type)
    safe_title = _redact_text(title) or title
    safe_content = _redact_text(content_json) or "{}"
    safe_created_by = _redact_text(created_by) if created_by else created_by
    async with _get_db() as db:
        await db.execute(
            """INSERT INTO artifacts
               (id, task_id, workspace, artifact_type, title, current_version, created_by)
               VALUES (?,?,?,?,?,1,?)""",
            (artifact_id, task_id, safe_workspace, safe_type, safe_title, safe_created_by),
        )
        await db.execute(
            """INSERT INTO artifact_versions
               (id, artifact_id, version_num, content_json, created_by)
               VALUES (?,?,?,?,?)""",
            (version_id, artifact_id, 1, safe_content, safe_created_by),
        )
    artifact = await get_artifact(artifact_id)
    if artifact is None:
        raise ValueError("artifact was not created")
    return artifact


async def add_artifact_version(
    artifact_id: str,
    content_json: str,
    created_by: Optional[str] = None,
    change_note: Optional[str] = None,
) -> Dict:
    safe_content = _redact_text(content_json) or "{}"
    safe_created_by = _redact_text(created_by) if created_by else created_by
    safe_change_note = _redact_text(change_note) if change_note else change_note
    async with _get_db() as db:
        row = await (await db.execute(
            "SELECT current_version FROM artifacts WHERE id=?",
            (artifact_id,),
        )).fetchone()
        if row is None:
            raise ValueError("artifact not found")
        version_num = int(row["current_version"]) + 1
        await db.execute(
            """INSERT INTO artifact_versions
               (id, artifact_id, version_num, content_json, created_by, change_note)
               VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), artifact_id, version_num, safe_content, safe_created_by, safe_change_note),
        )
        await db.execute(
            "UPDATE artifacts SET current_version=?, updated_at=? WHERE id=?",
            (version_num, _now(), artifact_id),
        )
    artifact = await get_artifact(artifact_id)
    if artifact is None:
        raise ValueError("artifact not found")
    return artifact


async def list_artifacts(task_id: Optional[str] = None, workspace: Optional[str] = None) -> List[Dict]:
    filters = []
    params = []
    if task_id:
        filters.append("task_id=?")
        params.append(task_id)
    if workspace:
        filters.append("workspace=?")
        params.append(normalize_workspace(workspace))
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    async with _get_db() as db:
        rows = await (await db.execute(
            f"SELECT * FROM artifacts {where} ORDER BY created_at DESC",
            tuple(params),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_artifact(artifact_id: str) -> Optional[Dict]:
    async with _get_db() as db:
        artifact = await (await db.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,))).fetchone()
        if not artifact:
            return None
        versions = await (await db.execute(
            "SELECT * FROM artifact_versions WHERE artifact_id=? ORDER BY version_num ASC",
            (artifact_id,),
        )).fetchall()
    result = dict(artifact)
    result["versions"] = [dict(row) for row in versions]
    return result


# ---------------------------------------------------------------------------
# Spend aggregates (A8, A9)
# ---------------------------------------------------------------------------

async def get_daily_spend(workspace: str = "personal") -> float:
    """Sum usd_spent for one workspace since UTC midnight today (A8)."""
    workspace = normalize_workspace(workspace)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with _get_db() as db:
        row = await (await db.execute(
            "SELECT COALESCE(SUM(usd_spent), 0) FROM tasks WHERE workspace=? AND created_at >= ?",
            (workspace, today),
        )).fetchone()
    return float(row[0]) if row else 0.0


async def get_summary(workspace: str = "personal") -> Dict:
    """Return task count and spend for today and the last 7 days (A9)."""
    workspace = normalize_workspace(workspace)
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    async with _get_db() as db:
        t_row = await (await db.execute(
            """SELECT COUNT(*) as n,
                      COALESCE(SUM(usd_spent), 0) as usd,
                      SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END) as succeeded,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
               FROM tasks WHERE workspace=? AND created_at >= ?""",
            (workspace, today),
        )).fetchone()
        w_row = await (await db.execute(
            """SELECT COUNT(*) as n,
                      COALESCE(SUM(usd_spent), 0) as usd,
                      SUM(CASE WHEN status='done'   THEN 1 ELSE 0 END) as succeeded,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
               FROM tasks WHERE workspace=? AND created_at >= ?""",
            (workspace, week_ago),
        )).fetchone()
    return {
        "workspace": workspace,
        "tasks_today":        int(t_row["n"])        if t_row else 0,
        "tasks_this_week":    int(w_row["n"])        if w_row else 0,
        "succeeded_today":    int(t_row["succeeded"] or 0) if t_row else 0,
        "failed_today":       int(t_row["failed"] or 0)    if t_row else 0,
        "succeeded_this_week": int(w_row["succeeded"] or 0) if w_row else 0,
        "failed_this_week":   int(w_row["failed"] or 0)    if w_row else 0,
        "total_usd_today":    round(float(t_row["usd"]) if t_row else 0.0, 4),
        "total_usd_this_week": round(float(w_row["usd"]) if w_row else 0.0, 4),
    }


# ---------------------------------------------------------------------------
# Health (F45)
# ---------------------------------------------------------------------------

async def health_check() -> bool:
    try:
        async with _get_db() as db:
            await db.execute("SELECT 1")
        return True
    except Exception:
        return False


async def health_detail() -> Dict:
    """Return DB health with enough detail for operators to spot missing/corrupt DBs."""
    db_path = os.path.abspath(settings.db_path)
    exists = os.path.exists(db_path)
    detail = {"ok": False, "path": db_path, "exists": exists, "error": ""}
    try:
        async with _get_db() as db:
            row = await (await db.execute("PRAGMA integrity_check")).fetchone()
        integrity = row[0] if row else "unknown"
        detail.update({"ok": integrity == "ok", "integrity": integrity})
    except Exception as exc:
        detail["error"] = str(exc)
    return detail


async def backup_database(retention_days: Optional[int] = None) -> Optional[str]:
    """Create a SQLite backup and prune old backups. Returns backup path or None."""
    if not settings.backup_enabled:
        return None
    db_path = os.path.abspath(settings.db_path)
    if not os.path.exists(db_path):
        return None
    retention_days = settings.backup_retention_days if retention_days is None else retention_days
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup_path = os.path.join(backup_dir, f"agent-{stamp}.db")

    def _copy() -> None:
        with sqlite3.connect(db_path) as src, sqlite3.connect(backup_path) as dst:
            src.backup(dst)

    import asyncio
    await asyncio.to_thread(_copy)

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    for name in os.listdir(backup_dir):
        if not name.startswith("agent-") or not name.endswith(".db"):
            continue
        path = os.path.join(backup_dir, name)
        mtime = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
        if mtime < cutoff:
            try:
                os.remove(path)
            except OSError:
                pass
    return backup_path


async def backup_status() -> Dict:
    """Return local SQLite backup status without exposing environment variables."""
    db_path = os.path.abspath(settings.db_path)
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    status = {
        "enabled": settings.backup_enabled,
        "retention_days": settings.backup_retention_days,
        "backup_dir_exists": os.path.isdir(backup_dir),
        "latest_backup": None,
        "backup_count": 0,
    }
    if not os.path.isdir(backup_dir):
        return status

    backups = []
    for name in os.listdir(backup_dir):
        if not name.startswith("agent-") or not name.endswith(".db"):
            continue
        path = os.path.join(backup_dir, name)
        try:
            backups.append((os.path.getmtime(path), name, os.path.getsize(path)))
        except OSError:
            continue
    status["backup_count"] = len(backups)
    if backups:
        mtime, name, size = max(backups)
        status["latest_backup"] = {
            "name": name,
            "created_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
            "size_bytes": size,
        }
    return status


# ---------------------------------------------------------------------------
# Metrics aggregation (A1)
# ---------------------------------------------------------------------------

def _latency_stats(durations_s: List[float]) -> Dict:
    """Return mean, median and p95 latency in seconds from a list of durations.

    Uses the nearest-rank method: p95 index = ceil(n * 0.95) - 1, clamped to [0, n-1].
    """
    if not durations_s:
        return {"median_s": None, "p95_s": None, "avg_s": None, "count": 0}
    import math
    sorted_d = sorted(durations_s)
    n = len(sorted_d)
    avg = sum(sorted_d) / n
    median = sorted_d[n // 2] if n % 2 == 1 else (sorted_d[n // 2 - 1] + sorted_d[n // 2]) / 2.0
    p95_idx = min(n - 1, max(0, math.ceil(n * 0.95) - 1))
    p95 = sorted_d[p95_idx]
    return {
        "median_s": round(median, 3),
        "p95_s": round(p95, 3),
        "avg_s": round(avg, 3),
        "count": n,
    }


async def get_metrics(window_hours: int = 24) -> Dict:
    """Return aggregated reliability and cost metrics for the given time window (A1).

    Metrics derived from explicit schema fields (started_at, ended_at,
    failure_category, usd_spent) — not from free-text parsing.

    Args:
        window_hours: lookback window in hours (24 or 168 for 7 days).

    Returns a dict with:
        - success_rate: fraction of non-cancelled finished tasks that succeeded
        - failure_rate: fraction of non-cancelled finished tasks that failed
        - latency: median/p95 latency for completed tasks (started_at → ended_at)
        - failure_categories: distribution of failure_category values
        - cost_summary: total and per-task-average USD spend
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    async with _get_db() as conn:
        rows = await (await conn.execute(
            """SELECT status, failure_category, started_at, ended_at,
                      COALESCE(usd_spent, 0) as usd_spent
               FROM tasks
               WHERE created_at >= ?""",
            (cutoff,),
        )).fetchall()

    total = len(rows)
    succeeded = sum(1 for r in rows if r["status"] == "done")
    failed = sum(1 for r in rows if r["status"] == "failed")
    cancelled = sum(1 for r in rows if r["status"] == "cancelled")
    # Rates exclude cancelled tasks from the denominator
    finished_non_cancelled = succeeded + failed
    success_rate = round(succeeded / finished_non_cancelled, 4) if finished_non_cancelled else None
    failure_rate = round(failed / finished_non_cancelled, 4) if finished_non_cancelled else None

    # Latency: only tasks with both started_at and ended_at
    durations: List[float] = []
    for r in rows:
        if r["started_at"] and r["ended_at"]:
            try:
                start = datetime.fromisoformat(r["started_at"])
                end = datetime.fromisoformat(r["ended_at"])
                delta = (end - start).total_seconds()
                if delta >= 0:
                    durations.append(delta)
            except (ValueError, TypeError):
                pass

    # Failure category distribution (only for failed tasks)
    category_counts: Dict[str, int] = {}
    for r in rows:
        if r["status"] == "failed":
            cat = r["failure_category"] or "unknown"
            category_counts[cat] = category_counts.get(cat, 0) + 1

    # Cost summary
    total_usd = round(sum(float(r["usd_spent"]) for r in rows), 6)
    avg_usd = round(total_usd / total, 6) if total else None

    return {
        "window_hours": window_hours,
        "total_tasks": total,
        "succeeded": succeeded,
        "failed": failed,
        "cancelled": cancelled,
        "success_rate": success_rate,
        "failure_rate": failure_rate,
        "latency": _latency_stats(durations),
        "failure_categories": category_counts,
        "cost_summary": {
            "total_usd": total_usd,
            "avg_usd_per_task": avg_usd,
        },
    }


# ---------------------------------------------------------------------------
# Growth: projects, revenue, clients, analytics, public tasks, activity
# ---------------------------------------------------------------------------

_CLIENT_UPDATABLE = frozenset({"name", "github_org", "budget_usd"})


async def create_project(
    name: str,
    workspace: str = "personal",
    description: Optional[str] = None,
) -> Dict:
    workspace = normalize_workspace(workspace)
    pid = str(uuid.uuid4())
    safe_name = _redact_text(name) or name
    safe_desc = _redact_text(description) if description else description
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO projects (id, workspace, name, description) VALUES (?,?,?,?)",
            (pid, workspace, safe_name, safe_desc),
        )
    return {"id": pid, "workspace": workspace, "name": safe_name, "description": safe_desc, "created_at": _now()}


async def list_projects(workspace: str = "personal", limit: int = 200) -> List[Dict]:
    workspace = normalize_workspace(workspace)
    limit = max(1, min(int(limit), 500))
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM projects WHERE workspace=? ORDER BY created_at DESC LIMIT ?",
            (workspace, limit),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_project(project_id: str, workspace: Optional[str] = None) -> Optional[Dict]:
    async with _get_db() as db:
        if workspace is None:
            row = await (await db.execute("SELECT * FROM projects WHERE id=?", (project_id,))).fetchone()
        else:
            w = normalize_workspace(workspace)
            row = await (await db.execute(
                "SELECT * FROM projects WHERE id=? AND workspace=?", (project_id, w)
            )).fetchone()
    return dict(row) if row else None


async def create_revenue_log(
    workspace: str,
    amount_usd: float,
    source: str,
    note: Optional[str] = None,
    project_id: Optional[str] = None,
) -> Dict:
    workspace = normalize_workspace(workspace)
    rid = str(uuid.uuid4())
    safe_source = _redact_text(source) or source
    safe_note = _redact_text(note) if note else note
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO revenue_log (id, workspace, project_id, amount_usd, source, note) VALUES (?,?,?,?,?,?)",
            (rid, workspace, project_id, float(amount_usd), safe_source, safe_note),
        )
    return {
        "id": rid,
        "workspace": workspace,
        "project_id": project_id,
        "amount_usd": float(amount_usd),
        "source": safe_source,
        "note": safe_note,
        "created_at": _now(),
    }


async def list_revenue_logs(workspace: str = "personal", limit: int = 200) -> List[Dict]:
    workspace = normalize_workspace(workspace)
    limit = max(1, min(int(limit), 500))
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM revenue_log WHERE workspace=? ORDER BY created_at DESC LIMIT ?",
            (workspace, limit),
        )).fetchall()
    return [dict(r) for r in rows]


async def revenue_totals_by_source(workspace: str = "personal") -> Dict:
    workspace = normalize_workspace(workspace)
    async with _get_db() as db:
        rows = await (await db.execute(
            """SELECT source, SUM(amount_usd) as total, COUNT(*) as n
               FROM revenue_log WHERE workspace=? GROUP BY source ORDER BY total DESC""",
            (workspace,),
        )).fetchall()
        grand = await (await db.execute(
            "SELECT COALESCE(SUM(amount_usd),0) as t FROM revenue_log WHERE workspace=?",
            (workspace,),
        )).fetchone()
    total_all = float(grand["t"]) if grand else 0.0
    by_source = [{"source": r["source"], "total_usd": round(float(r["total"]), 2), "count": int(r["n"])} for r in rows]
    return {"workspace": workspace, "total_usd": round(total_all, 2), "by_source": by_source}


async def create_client(
    name: str,
    workspace: str = "work",
    github_org: Optional[str] = None,
    budget_usd: Optional[float] = None,
) -> Dict:
    workspace = normalize_workspace(workspace)
    cid = str(uuid.uuid4())
    safe_name = _redact_text(name) or name
    safe_org = _redact_text(github_org) if github_org else github_org
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO clients (id, workspace, name, github_org, budget_usd) VALUES (?,?,?,?,?)",
            (cid, workspace, safe_name, safe_org, budget_usd),
        )
    return {
        "id": cid,
        "workspace": workspace,
        "name": safe_name,
        "github_org": safe_org,
        "budget_usd": budget_usd,
        "created_at": _now(),
    }


async def list_clients(workspace: str = "work", limit: int = 200) -> List[Dict]:
    workspace = normalize_workspace(workspace)
    limit = max(1, min(int(limit), 500))
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM clients WHERE workspace=? ORDER BY created_at DESC LIMIT ?",
            (workspace, limit),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_client(client_id: str, workspace: Optional[str] = None) -> Optional[Dict]:
    async with _get_db() as db:
        if workspace is None:
            row = await (await db.execute("SELECT * FROM clients WHERE id=?", (client_id,))).fetchone()
        else:
            w = normalize_workspace(workspace)
            row = await (await db.execute(
                "SELECT * FROM clients WHERE id=? AND workspace=?", (client_id, w)
            )).fetchone()
    return dict(row) if row else None


async def update_client(client_id: str, workspace: str, **kwargs) -> Optional[Dict]:
    kwargs = _safe_cols(_CLIENT_UPDATABLE, kwargs)
    if "name" in kwargs:
        kwargs["name"] = _redact_text(kwargs["name"]) or kwargs["name"]
    if "github_org" in kwargs and kwargs["github_org"] is not None:
        kwargs["github_org"] = _redact_text(kwargs["github_org"])
    if not kwargs:
        return await get_client(client_id, workspace=workspace)
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [client_id, normalize_workspace(workspace)]
    async with _get_db() as db:
        await db.execute(
            f"UPDATE clients SET {sets} WHERE id=? AND workspace=?",
            tuple(vals),
        )
    return await get_client(client_id, workspace=workspace)


async def delete_client(client_id: str, workspace: str) -> bool:
    workspace = normalize_workspace(workspace)
    async with _get_db() as db:
        cur = await db.execute("DELETE FROM clients WHERE id=? AND workspace=?", (client_id, workspace))
        return cur.rowcount > 0


async def count_steps(task_id: str) -> int:
    async with _get_db() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) as c FROM steps WHERE task_id=?", (task_id,)
        )).fetchone()
    return int(row["c"]) if row else 0


async def get_public_task(task_id: str) -> Optional[Dict]:
    async with _get_db() as db:
        row = await (await db.execute(
            "SELECT id, title, status, pr_url, public FROM tasks WHERE id=?", (task_id,)
        )).fetchone()
    if not row or not int(row["public"] or 0):
        return None
    n = await count_steps(task_id)
    return {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
        "pr_url": row["pr_url"],
        "step_count": n,
    }


async def list_public_tasks_done(limit: int = 50) -> List[Dict]:
    limit = max(1, min(int(limit), 200))
    async with _get_db() as db:
        rows = await (await db.execute(
            """SELECT t.id, t.title, t.status, t.pr_url, t.usd_spent, t.created_at, t.ended_at, t.workspace,
                      (SELECT COUNT(*) FROM steps s WHERE s.task_id = t.id) as step_count
               FROM tasks t
               WHERE COALESCE(t.public,0)=1 AND t.status='done'
               ORDER BY datetime(COALESCE(t.updated_at, t.created_at)) DESC LIMIT ?""",
            (limit,),
        )).fetchall()
    return [dict(r) for r in rows]


async def list_recent_activity(limit: int = 20) -> List[Dict]:
    limit = max(1, min(int(limit), 100))
    async with _get_db() as db:
        rows = await (await db.execute(
            """SELECT l.id, l.task_id, l.level, l.message, l.created_at, t.title as task_title, t.workspace
               FROM logs l
               JOIN tasks t ON t.id = l.task_id
               ORDER BY datetime(l.created_at) DESC LIMIT ?""",
            (limit,),
        )).fetchall()
    return [dict(r) for r in rows]


async def get_analytics(workspace: Optional[str] = None) -> Dict:
    """Operator growth dashboard: last 30 days tasks, costs, PRs, tools."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    scoped = workspace and str(workspace).lower() != "all"
    async with _get_db() as db:
        if scoped:
            w = normalize_workspace(str(workspace))
            day_rows = await (await db.execute(
                """SELECT substr(created_at, 1, 10) as day, COUNT(*) as n
                   FROM tasks WHERE created_at >= ? AND workspace=?
                   GROUP BY day ORDER BY day""",
                (cutoff, w),
            )).fetchall()
            stat_row = await (await db.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done_n,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as fail_n,
                          COALESCE(AVG(CASE WHEN status IN ('done','failed') THEN usd_spent END), 0) as avg_cost,
                          SUM(CASE WHEN pr_url IS NOT NULL AND pr_url != '' THEN 1 ELSE 0 END) as prs
                   FROM tasks WHERE created_at >= ? AND workspace=?""",
                (cutoff, w),
            )).fetchone()
            tool_rows = await (await db.execute(
                """SELECT s.tool_name, COUNT(*) as c
                   FROM steps s JOIN tasks t ON t.id = s.task_id
                   WHERE s.created_at >= ? AND t.workspace=?
                     AND s.tool_name IS NOT NULL AND s.tool_name != ''
                   GROUP BY s.tool_name ORDER BY c DESC LIMIT 12""",
                (cutoff, w),
            )).fetchall()
            filt = w
        else:
            day_rows = await (await db.execute(
                """SELECT substr(created_at, 1, 10) as day, COUNT(*) as n
                   FROM tasks WHERE created_at >= ?
                   GROUP BY day ORDER BY day""",
                (cutoff,),
            )).fetchall()
            stat_row = await (await db.execute(
                """SELECT COUNT(*) as total,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) as done_n,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as fail_n,
                          COALESCE(AVG(CASE WHEN status IN ('done','failed') THEN usd_spent END), 0) as avg_cost,
                          SUM(CASE WHEN pr_url IS NOT NULL AND pr_url != '' THEN 1 ELSE 0 END) as prs
                   FROM tasks WHERE created_at >= ?""",
                (cutoff,),
            )).fetchone()
            tool_rows = await (await db.execute(
                """SELECT s.tool_name, COUNT(*) as c
                   FROM steps s JOIN tasks t ON t.id = s.task_id
                   WHERE s.created_at >= ? AND s.tool_name IS NOT NULL AND s.tool_name != ''
                   GROUP BY s.tool_name ORDER BY c DESC LIMIT 12""",
                (cutoff,),
            )).fetchall()
            filt = "all"

    tasks_per_day = [{"day": r["day"], "count": int(r["n"])} for r in day_rows]
    total = int(stat_row["total"]) if stat_row else 0
    done_n = int(stat_row["done_n"] or 0) if stat_row else 0
    fail_n = int(stat_row["fail_n"] or 0) if stat_row else 0
    finished = done_n + fail_n
    success_rate = round(done_n / finished, 4) if finished else None
    most_used_tools = [{"tool": r["tool_name"], "count": int(r["c"])} for r in tool_rows]

    return {
        "workspace_filter": filt,
        "tasks_per_day": tasks_per_day,
        "avg_cost_per_task_usd": round(float(stat_row["avg_cost"] or 0), 4) if stat_row else 0.0,
        "total_tasks_window": total,
        "total_prs_with_url": int(stat_row["prs"] or 0) if stat_row else 0,
        "success_rate": success_rate,
        "most_used_tools": most_used_tools,
    }
