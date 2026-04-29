"""Central event bus — asyncio.Queue per task_id."""
import asyncio
from typing import Dict, Any

_buses: Dict[str, asyncio.Queue] = {}


def get_bus(task_id: str) -> asyncio.Queue:
    if task_id not in _buses:
        _buses[task_id] = asyncio.Queue()
    return _buses[task_id]


def destroy_bus(task_id: str) -> None:
    _buses.pop(task_id, None)


async def emit(task_id: str, event_type: str, data: Any) -> None:
    bus = get_bus(task_id)
    await bus.put({"type": event_type, "data": data})


async def emit_log(task_id: str, level: str, message: str, **kwargs) -> None:
    await emit(task_id, "log", {"level": level, "message": message, **kwargs})


async def emit_step(task_id: str, step: int, tool: str, status: str, **kwargs) -> None:
    await emit(task_id, "step", {"step": step, "tool": tool, "status": status, **kwargs})


async def emit_status(task_id: str, status: str, **kwargs) -> None:
    await emit(task_id, "status", {"status": status, **kwargs})
