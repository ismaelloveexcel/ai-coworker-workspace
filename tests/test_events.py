"""Tests for backend.events — multi-subscriber fan-out and bounded queues."""
import pytest

from backend.events import MAX_QUEUE, destroy_bus, emit, subscribe, unsubscribe


@pytest.mark.asyncio
async def test_subscribe_creates_queue():
    bus = subscribe("test-task-1")
    assert bus.maxsize == MAX_QUEUE
    destroy_bus("test-task-1")


@pytest.mark.asyncio
async def test_two_subscribers_receive_same_event():
    task_id = "test-task-2"
    first = subscribe(task_id)
    second = subscribe(task_id)
    await emit(task_id, "log", {"message": "hello"})

    first_event = first.get_nowait()
    second_event = second.get_nowait()
    # Both subscribers receive the same type, data, and sequence number.
    assert first_event["type"] == second_event["type"] == "log"
    assert first_event["data"] == second_event["data"] == {"message": "hello"}
    assert first_event["seq"] == second_event["seq"] == 1
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_unsubscribe_one_subscriber_leaves_other_active():
    task_id = "test-task-unsubscribe"
    first = subscribe(task_id)
    second = subscribe(task_id)
    unsubscribe(task_id, first)

    await emit(task_id, "log", {"message": "still here"})

    assert first.empty()
    assert second.get_nowait()["data"]["message"] == "still here"
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_emit_with_no_subscribers_succeeds():
    task_id = "test-task-no-subscribers"
    await emit(task_id, "log", {"message": "nobody home"})

    from backend.events import _subscribers
    assert task_id not in _subscribers


@pytest.mark.asyncio
async def test_queue_bounded_drops_oldest():
    task_id = "test-task-bounded"
    bus = subscribe(task_id)
    # Fill queue to max
    for i in range(MAX_QUEUE):
        await emit(task_id, "log", {"i": i})
    # One more — should drop 2 oldest, add event + stream_warning, not raise
    await emit(task_id, "log", {"i": MAX_QUEUE})
    # Queue should still be at most MAX_QUEUE items
    assert bus.qsize() <= MAX_QUEUE
    # Items 0 and 1 were dropped to make room; item 2 is now first
    assert bus.get_nowait()["data"]["i"] == 2
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_destroy_removes_bus():
    task_id = "test-task-destroy"
    subscribe(task_id)
    destroy_bus(task_id)
    from backend.events import _subscribers
    assert task_id not in _subscribers


# ---------------------------------------------------------------------------
# A2a: sequence IDs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_events_have_seq_field():
    task_id = "test-task-seq"
    bus = subscribe(task_id)
    await emit(task_id, "log", {"n": 1})
    await emit(task_id, "log", {"n": 2})
    await emit(task_id, "log", {"n": 3})
    events = [bus.get_nowait() for _ in range(3)]
    seqs = [e["seq"] for e in events]
    assert seqs == [1, 2, 3], f"Expected [1,2,3] got {seqs}"
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_seq_monotonically_increases_across_types():
    task_id = "test-task-seq-types"
    bus = subscribe(task_id)
    await emit(task_id, "log", {"msg": "a"})
    await emit(task_id, "task_done", {"ok": True})
    first = bus.get_nowait()
    second = bus.get_nowait()
    assert first["seq"] < second["seq"]
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_seq_independent_per_task():
    task_a = "test-seq-task-a"
    task_b = "test-seq-task-b"
    bus_a = subscribe(task_a)
    bus_b = subscribe(task_b)
    await emit(task_a, "log", {})
    await emit(task_a, "log", {})
    await emit(task_b, "log", {})
    assert bus_a.get_nowait()["seq"] == 1
    assert bus_a.get_nowait()["seq"] == 2
    assert bus_b.get_nowait()["seq"] == 1
    destroy_bus(task_a)
    destroy_bus(task_b)


# ---------------------------------------------------------------------------
# A2a: dropped counter + stream_warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overflow_increments_dropped_counter():
    from backend.events import _dropped
    task_id = "test-task-dropped-counter"
    bus = subscribe(task_id)
    # Fill queue to capacity
    for i in range(MAX_QUEUE):
        await emit(task_id, "log", {"i": i})
    initial_dropped = _dropped.get(id(bus), 0)
    assert initial_dropped == 0
    # Trigger overflow
    await emit(task_id, "log", {"i": MAX_QUEUE})
    # Two items were evicted from the queue: one for the triggering event, one for the warning.
    assert _dropped.get(id(bus), 0) == 2
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_overflow_emits_stream_warning():
    task_id = "test-task-stream-warning"
    bus = subscribe(task_id)
    # Fill queue to capacity
    for i in range(MAX_QUEUE):
        await emit(task_id, "log", {"i": i})
    # Trigger overflow
    await emit(task_id, "log", {"i": MAX_QUEUE})
    # Drain to find the stream_warning
    events = []
    while not bus.empty():
        events.append(bus.get_nowait())
    warning_events = [e for e in events if e["type"] == "stream_warning"]
    assert len(warning_events) == 1
    warning = warning_events[0]
    assert warning["data"]["dropped"] == 2  # two items evicted: slot for event + slot for warning
    assert warning["data"]["task_id"] == task_id
    assert "seq" in warning
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_dropped_counter_cleared_on_destroy():
    from backend.events import _dropped
    task_id = "test-task-dropped-cleanup"
    bus = subscribe(task_id)
    queue_id = id(bus)
    destroy_bus(task_id)
    assert queue_id not in _dropped


@pytest.mark.asyncio
async def test_dropped_counter_cleared_on_unsubscribe():
    from backend.events import _dropped
    task_id = "test-task-dropped-unsub"
    bus = subscribe(task_id)
    queue_id = id(bus)
    unsubscribe(task_id, bus)
    assert queue_id not in _dropped

