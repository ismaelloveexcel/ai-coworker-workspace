"""
SQLite database layer — WAL mode, auto-init, async-safe.
Schema is idempotent (CREATE TABLE IF NOT EXISTS).
"""
import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiosqlite

from backend.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    db = await aiosqlite.connect(settings.db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA synchronous=NORMAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db() -> None:
    """Idempotent schema creation."""
    db = await _get_db()
    async with db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                prompt      TEXT NOT NULL,
                repo_url    TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                branch_name TEXT,
                pr_url      TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 2,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS steps (
                id          TEXT PRIMARY KEY,
                task_id     TEXT NOT NULL REFERENCES tasks(id),
                step_num    INTEGER NOT NULL,
                tool_name   TEXT,
                tool_input  TEXT,
                tool_output TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                plan        TEXT,
                reasoning   TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS logs (
                id          TEXT PRIMARY KEY,
                task_id     TEXT REFERENCES tasks(id),
                level       TEXT NOT NULL DEFAULT 'info',
                message     TEXT NOT NULL,
                meta        TEXT,
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_steps_task_id ON steps(task_id);
            CREATE INDEX IF NOT EXISTS idx_logs_task_id  ON logs(task_id);
        """)


# ── Tasks ──────────────────────────────────────────────────────────────────────

async def create_task(title: str, prompt: str, repo_url: str = None) -> Dict:
    task_id = str(uuid.uuid4())
    now = _now()
    db = await _get_db()
    async with db:
        await db.execute(
            """INSERT INTO tasks (id, title, prompt, repo_url, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (task_id, title, prompt, repo_url, now, now),
        )
    return await get_task(task_id)


async def get_task(task_id: str) -> Optional[Dict]:
    db = await _get_db()
    async with db:
        async with db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_tasks() -> List[Dict]:
    db = await _get_db()
    async with db:
        async with db.execute("SELECT * FROM tasks ORDER BY created_at DESC") as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def update_task(task_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [task_id]
    db = await _get_db()
    async with db:
        await db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", vals)


async def delete_task(task_id: str) -> None:
    db = await _get_db()
    async with db:
        await db.execute("DELETE FROM steps WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM logs  WHERE task_id = ?", (task_id,))
        await db.execute("DELETE FROM tasks WHERE id = ?",      (task_id,))


# ── Steps ──────────────────────────────────────────────────────────────────────

async def create_step(task_id: str, step_num: int, plan: str = None) -> str:
    step_id = str(uuid.uuid4())
    now = _now()
    db = await _get_db()
    async with db:
        await db.execute(
            """INSERT INTO steps (id, task_id, step_num, plan, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?)""",
            (step_id, task_id, step_num, plan, now, now),
        )
    return step_id


async def update_step(step_id: str, **kwargs) -> None:
    if not kwargs:
        return
    kwargs["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [step_id]
    db = await _get_db()
    async with db:
        await db.execute(f"UPDATE steps SET {cols} WHERE id = ?", vals)


async def get_steps(task_id: str) -> List[Dict]:
    db = await _get_db()
    async with db:
        async with db.execute(
            "SELECT * FROM steps WHERE task_id = ? ORDER BY step_num", (task_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ── Logs ───────────────────────────────────────────────────────────────────────

async def add_log(task_id: str, level: str, message: str, meta: str = None) -> None:
    db = await _get_db()
    async with db:
        await db.execute(
            "INSERT INTO logs (id, task_id, level, message, meta, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), task_id, level, message, meta, _now()),
        )


async def get_logs(task_id: str) -> List[Dict]:
    db = await _get_db()
    async with db:
        async with db.execute(
            "SELECT * FROM logs WHERE task_id = ? ORDER BY created_at", (task_id,)
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
