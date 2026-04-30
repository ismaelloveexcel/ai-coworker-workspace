"""
Database layer — SQLite WAL mode via aiosqlite.

v2: F18/E10 schema versioning + migrations, F19 column allowlist,
    F17 proper async context manager, F29 paginated list_tasks,
    F35 correct Optional typing, F45 health_check().
    Added usd_spent / heartbeat_at columns (operator survival kit).
"""
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import aiosqlite

from backend.config import settings

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

# ---------------------------------------------------------------------------
# Column allowlists (F19)
# ---------------------------------------------------------------------------

_TASK_UPDATABLE = frozenset({"status", "pr_url", "error", "updated_at", "heartbeat_at", "usd_spent"})
_STEP_UPDATABLE = frozenset({"status", "tool_name", "tool_input", "tool_output", "reasoning", "updated_at"})


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
    """Single-use WAL connection; commits on clean exit, rolls back on error."""
    os.makedirs(os.path.dirname(os.path.abspath(settings.db_path)), exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
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
    os.makedirs(os.path.dirname(os.path.abspath(settings.db_path)), exist_ok=True)
    async with aiosqlite.connect(settings.db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        row = await (await db.execute("SELECT COALESCE(MAX(version),0) FROM schema_version")).fetchone()
        current = row[0]
        for idx, sql in enumerate(_MIGRATIONS, 1):
            if idx > current:
                await db.executescript(sql)
                await db.execute("INSERT INTO schema_version (version) VALUES (?)", (idx,))
        await db.commit()


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_task(title: str, prompt: str, repo_url: Optional[str] = None) -> Dict:
    task_id = str(uuid.uuid4())
    async with _get_db() as db:
        await db.execute(
            "INSERT INTO tasks (id, title, prompt, repo_url, status) VALUES (?,?,?,?,'pending')",
            (task_id, title, prompt, repo_url),
        )
    return {"id": task_id, "title": title, "prompt": prompt, "repo_url": repo_url, "status": "pending", "created_at": _now()}


async def get_task(task_id: str) -> Optional[Dict]:
    async with _get_db() as db:
        row = await (await db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))).fetchone()
    return dict(row) if row else None


async def list_tasks(limit: int = 50, offset: int = 0) -> List[Dict]:
    """Paginated — F29."""
    async with _get_db() as db:
        rows = await (await db.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
        )).fetchall()
    return [dict(r) for r in rows]


async def update_task(task_id: str, **kwargs) -> None:
    kwargs = _safe_cols(_TASK_UPDATABLE, kwargs)
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


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------

async def add_log(task_id: str, level: str, message: str,
                  meta: Optional[str] = None) -> None:
    log_id = str(uuid.uuid4())
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
