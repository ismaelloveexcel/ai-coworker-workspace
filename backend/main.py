"""
FastAPI backend — full async, SSE streaming, task lifecycle.
"""
import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Optional

import structlog
import uvloop
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend import db
from backend.agent_loop import run_task
from backend.events import destroy_bus, emit_log, get_bus

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    log.info("DB initialized")
    yield
    log.info("Shutting down")


app = FastAPI(title="AI Coworker", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Running tasks registry ─────────────────────────────────────────────────────
_running_tasks: dict = {}


# ── Models ─────────────────────────────────────────────────────────────────────

class CreateTaskRequest(BaseModel):
    title: str
    prompt: str
    repo_url: Optional[str] = None


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "service": "ai-coworker"}


@app.post("/tasks", status_code=201)
async def create_task(req: CreateTaskRequest, background_tasks: BackgroundTasks):
    task = await db.create_task(req.title, req.prompt, req.repo_url)
    task_id = task["id"]
    background_tasks.add_task(_run_task_bg, task_id)
    return task


async def _run_task_bg(task_id: str):
    asyncio_task = asyncio.create_task(run_task(task_id))
    _running_tasks[task_id] = asyncio_task
    try:
        await asyncio_task
    finally:
        _running_tasks.pop(task_id, None)
        # Give SSE clients time to receive final events
        await asyncio.sleep(2)
        destroy_bus(task_id)


@app.get("/tasks")
async def list_tasks():
    return await db.list_tasks()


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    steps = await db.get_steps(task_id)
    logs = await db.get_logs(task_id)
    return {**task, "steps": steps, "logs": logs}


@app.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    # Cancel running asyncio task
    at = _running_tasks.get(task_id)
    if at and not at.done():
        at.cancel()

    await db.update_task(task_id, status="cancelled")
    await emit_log(task_id, "warn", "Task cancelled by user")
    return {"status": "cancelled"}


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    task = await db.get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        bus = get_bus(task_id)

        # Drain any already-queued events first
        while True:
            try:
                event = bus.get_nowait()
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.QueueEmpty:
                break

        # Stream new events
        while True:
            try:
                event = await asyncio.wait_for(bus.get(), timeout=30)
                yield f"data: {json.dumps(event)}\n\n"

                # Stop streaming once terminal status received
                if event.get("type") == "status" and event.get("data", {}).get("status") in ("done", "failed", "cancelled"):
                    yield "data: {\"type\": \"done\"}\n\n"
                    return

            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"  # SSE keepalive

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
