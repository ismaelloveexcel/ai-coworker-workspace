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
    assert first_event == second_event == {"type": "log", "data": {"message": "hello"}}
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
    # One more — should drop oldest, not raise
    await emit(task_id, "log", {"i": MAX_QUEUE})
    # Queue should still have MAX_QUEUE items (not MAX_QUEUE + 1)
    assert bus.qsize() <= MAX_QUEUE
    assert bus.get_nowait()["data"]["i"] == 1
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_destroy_removes_bus():
    task_id = "test-task-destroy"
    subscribe(task_id)
    destroy_bus(task_id)
    from backend.events import _subscribers
    assert task_id not in _subscribers

