"""
Event bus — one asyncio.Queue per task_id.

v2 fixes:
- F8:  Queue maxsize=500 prevents unbounded memory growth from slow SSE consumers
- F31: destroy_bus is called by agent_loop after a 5s grace period (not here)
- Queues are created lazily per-task and removed by destroy_bus
"""
import asyncio
from typing import Dict

_buses: Dict[str, asyncio.Queue] = {}

MAX_QUEUE = 500   # events; slow consumer gets drops, not OOM (F8)


def get_bus(task_id: str) -> asyncio.Queue:
    if task_id not in _buses:
        _buses[task_id] = asyncio.Queue(maxsize=MAX_QUEUE)
    return _buses[task_id]


def destroy_bus(task_id: str) -> None:
    """Remove bus. Call after agent task is fully done (with grace period)."""
    _buses.pop(task_id, None)


async def emit(task_id: str, event_type: str, data: dict) -> None:
    bus = get_bus(task_id)
    event = {"type": event_type, "data": data}
    try:
        bus.put_nowait(event)
    except asyncio.QueueFull:
        # Drop oldest event to make room (slow consumer — don't block agent)
        try:
            bus.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            bus.put_nowait(event)
        except asyncio.QueueFull:
            pass  # still full — drop this event


async def emit_log(task_id: str, level: str, message: str, **extra) -> None:
    data = {"level": level, "message": message}
    data.update(extra)
    await emit(task_id, "log", data)

