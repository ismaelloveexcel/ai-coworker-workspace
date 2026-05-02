"""Task event bus with one bounded queue per active subscriber."""
import asyncio
from typing import Dict, Set

_subscribers: Dict[str, Set[asyncio.Queue]] = {}
_sequence: Dict[str, int] = {}   # per-task monotonic sequence counter (A2a)
_dropped: Dict[int, int] = {}    # per-subscriber dropped-event counter keyed by id(queue) (A2a)

MAX_QUEUE = 500   # events; slow consumer gets drops, not OOM (F8)


def subscribe(task_id: str) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
    _subscribers.setdefault(task_id, set()).add(queue)
    _dropped[id(queue)] = 0
    return queue


def unsubscribe(task_id: str, queue: asyncio.Queue) -> None:
    subscribers = _subscribers.get(task_id)
    if not subscribers:
        return
    subscribers.discard(queue)
    _dropped.pop(id(queue), None)
    if not subscribers:
        _subscribers.pop(task_id, None)


def destroy_bus(task_id: str) -> None:
    """Remove all subscriber queues for a task after terminal-event grace period."""
    for queue in _subscribers.pop(task_id, set()):
        _dropped.pop(id(queue), None)
    _sequence.pop(task_id, None)


def _next_seq(task_id: str) -> int:
    """Return the next monotonically increasing sequence number for *task_id*."""
    seq = _sequence.get(task_id, 0) + 1
    _sequence[task_id] = seq
    return seq


async def emit(task_id: str, event_type: str, data: dict) -> None:
    seq = _next_seq(task_id)
    event = {"type": event_type, "data": data, "seq": seq}
    for queue in tuple(_subscribers.get(task_id, set())):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop 2 oldest events to make room for the new event and the warning.
            # The counter tracks every item physically removed from the queue.
            evicted = 0
            for _ in range(2):
                try:
                    queue.get_nowait()
                    evicted += 1
                except asyncio.QueueEmpty:
                    break
            _dropped[id(queue)] = _dropped.get(id(queue), 0) + evicted
            dropped_count = _dropped[id(queue)]
            # Insert the triggering event.
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
            # Insert a stream_warning to signal loss (non-blocking; may itself be
            # dropped under extreme back-pressure, but the dropped counter persists).
            # warn_seq is allocated here; a gap in seq indicates the warning was lost.
            warn_seq = _next_seq(task_id)
            warning_event = {
                "type": "stream_warning",
                "data": {"dropped": dropped_count, "task_id": task_id},
                "seq": warn_seq,
            }
            try:
                queue.put_nowait(warning_event)
            except asyncio.QueueFull:
                pass


async def emit_log(task_id: str, level: str, message: str, **extra) -> None:
    data = {"level": level, "message": message}
    data.update(extra)
    await emit(task_id, "log", data)

