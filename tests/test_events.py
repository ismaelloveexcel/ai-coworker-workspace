"""Tests for backend.events — bounded queue, drop-oldest on full."""
import asyncio
import pytest

import os
from backend.events import emit, get_bus, destroy_bus, MAX_QUEUE


@pytest.mark.asyncio
async def test_bus_created_on_demand():
    bus = get_bus("test-task-1")
    assert bus is not None
    destroy_bus("test-task-1")


@pytest.mark.asyncio
async def test_emit_puts_event():
    task_id = "test-task-2"
    bus = get_bus(task_id)
    await emit(task_id, "log", {"message": "hello"})
    event = bus.get_nowait()
    assert event["type"] == "log"
    assert event["data"]["message"] == "hello"
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_queue_bounded_drops_oldest():
    task_id = "test-task-bounded"
    bus = get_bus(task_id)
    # Fill queue to max
    for i in range(MAX_QUEUE):
        await emit(task_id, "log", {"i": i})
    # One more — should drop oldest, not raise
    await emit(task_id, "log", {"i": MAX_QUEUE})
    # Queue should still have MAX_QUEUE items (not MAX_QUEUE + 1)
    assert bus.qsize() <= MAX_QUEUE
    destroy_bus(task_id)


@pytest.mark.asyncio
async def test_destroy_removes_bus():
    task_id = "test-task-destroy"
    get_bus(task_id)
    destroy_bus(task_id)
    from backend.events import _buses
    assert task_id not in _buses

