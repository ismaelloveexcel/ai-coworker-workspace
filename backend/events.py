"""Task event bus with one bounded queue per active subscriber."""
import asyncio
from typing import Dict, Set

_subscribers: Dict[str, Set[asyncio.Queue]] = {}

MAX_QUEUE = 500   # events; slow consumer gets drops, not OOM (F8)


def subscribe(task_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
    _subscribers.setdefault(task_id, set()).add(queue)
    return queue


def unsubscribe(task_id: str, queue: asyncio.Queue) -> None:
    subscribers = _subscribers.get(task_id)
    if not subscribers:
        return
    subscribers.discard(queue)
    if not subscribers:
        _subscribers.pop(task_id, None)


def destroy_bus(task_id: str) -> None:
    """Remove all subscriber queues for a task after terminal-event grace period."""
    _subscribers.pop(task_id, None)


async def emit(task_id: str, event_type: str, data: dict) -> None:
    event = {"type": event_type, "data": data}
    for queue in tuple(_subscribers.get(task_id, set())):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass


async def emit_log(task_id: str, level: str, message: str, **extra) -> None:
    data = {"level": level, "message": message}
    data.update(extra)
    await emit(task_id, "log", data)

