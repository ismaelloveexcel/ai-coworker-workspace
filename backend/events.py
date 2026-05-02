"""Task event bus with one bounded queue per active subscriber."""
import asyncio
from typing import Dict, Set

_subscribers: Dict[str, Set[asyncio.Queue]] = {}
_sequence: Dict[str, int] = {}          # per-task monotonic sequence counter
_dropped: Dict[int, int] = {}           # per-queue drop counter keyed by id(queue)

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
        _sequence.pop(task_id, None)


def destroy_bus(task_id: str) -> None:
    """Remove all subscriber queues for a task after terminal-event grace period."""
    queues = _subscribers.pop(task_id, set())
    for q in queues:
        _dropped.pop(id(q), None)
    _sequence.pop(task_id, None)


async def emit(task_id: str, event_type: str, data: dict) -> None:
    seq = _sequence.get(task_id, 0) + 1
    _sequence[task_id] = seq
    event = {"type": event_type, "seq": seq, "data": data}
    for queue in tuple(_subscribers.get(task_id, set())):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop oldest event and track the loss
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            _dropped[id(queue)] = _dropped.get(id(queue), 0) + 1
            dropped_count = _dropped[id(queue)]
            # Drop one more event to reserve a slot for the stream_warning
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
            # Emit a stream_warning so the consumer knows events were lost
            warning_seq = _sequence.get(task_id, seq) + 1
            _sequence[task_id] = warning_seq
            warning = {
                "type": "stream_warning",
                "seq": warning_seq,
                "data": {"dropped": dropped_count, "last_seq": seq},
            }
            try:
                queue.put_nowait(warning)
            except asyncio.QueueFull:
                pass


async def emit_log(task_id: str, level: str, message: str, **extra) -> None:
    data = {"level": level, "message": message}
    data.update(extra)
    await emit(task_id, "log", data)

