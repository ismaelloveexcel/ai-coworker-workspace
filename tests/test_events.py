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
    # Both queues receive identical events including seq
    assert first_event == second_event
    assert first_event["type"] == "log"
    assert first_event["data"] == {"message": "hello"}
    assert first_event["seq"] == 1
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_events_have_monotonic_seq():
    task_id = "test-task-seq"
    bus = subscribe(task_id)
    for _ in range(3):
        await emit(task_id, "log", {"x": 1})
    seqs = [bus.get_nowait()["seq"] for _ in range(3)]
    assert seqs == [1, 2, 3]
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
async def test_queue_bounded_drops_oldest_and_emits_warning():
    task_id = "test-task-bounded"
    bus = subscribe(task_id)
    # Fill queue to max
    for i in range(MAX_QUEUE):
        await emit(task_id, "log", {"i": i})
    # One more — should drop two oldest (to fit event + warning), not raise
    await emit(task_id, "log", {"i": MAX_QUEUE})
    # Queue should still be at MAX_QUEUE (2 dropped, new event + warning added)
    assert bus.qsize() <= MAX_QUEUE
    # The two oldest events (i=0, i=1) were dropped; first item now is i=2
    first = bus.get_nowait()
    assert first["type"] == "log"
    assert first["data"]["i"] == 2
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_overflow_produces_stream_warning():
    """When a queue overflows, a stream_warning event must appear."""
    task_id = "test-task-warning"
    bus = subscribe(task_id)
    # Fill to capacity
    for i in range(MAX_QUEUE):
        await emit(task_id, "log", {"i": i})
    # Trigger one overflow
    await emit(task_id, "log", {"i": MAX_QUEUE})

    # Drain until we find the stream_warning
    events = []
    while not bus.empty():
        events.append(bus.get_nowait())
    warning_events = [e for e in events if e["type"] == "stream_warning"]
    assert warning_events, "Expected at least one stream_warning event after overflow"
    w = warning_events[0]
    assert w["data"]["dropped"] >= 1
    assert "last_seq" in w["data"]
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_destroy_removes_bus():
    task_id = "test-task-destroy"
    subscribe(task_id)
    destroy_bus(task_id)
    from backend.events import _subscribers
    assert task_id not in _subscribers


@pytest.mark.asyncio
async def test_destroy_cleans_up_sequence_and_dropped():
    task_id = "test-task-cleanup"
    bus = subscribe(task_id)
    await emit(task_id, "log", {"x": 1})
    destroy_bus(task_id)
    from backend.events import _sequence, _dropped
    assert task_id not in _sequence
    assert id(bus) not in _dropped

