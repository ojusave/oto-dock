"""Tests for per-machine install lock on SatelliteConnectionManager."""

import asyncio

import pytest


@pytest.mark.asyncio
async def test_get_install_lock_returns_same_instance():
    """Two calls for the same machine_id return the same Lock instance
    so concurrent callers serialize correctly."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    lock1 = cm.get_install_lock("m-1")
    lock2 = cm.get_install_lock("m-1")
    assert lock1 is lock2


@pytest.mark.asyncio
async def test_different_machines_get_different_locks():
    """Different machine_ids get independent locks (no cross-machine
    serialization)."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    assert cm.get_install_lock("m-1") is not cm.get_install_lock("m-2")


@pytest.mark.asyncio
async def test_lock_serializes_concurrent_acquirers():
    """Two coroutines trying to acquire the same machine's lock serialize."""
    from core.remote.satellite_connection import SatelliteConnectionManager
    cm = SatelliteConnectionManager()
    lock = cm.get_install_lock("m-1")

    order: list[str] = []

    async def worker(name: str, delay: float):
        async with lock:
            order.append(f"{name}:enter")
            await asyncio.sleep(delay)
            order.append(f"{name}:exit")

    # Start A, give it the lock, then start B — B must wait.
    task_a = asyncio.create_task(worker("A", 0.05))
    await asyncio.sleep(0.01)  # let A grab the lock first
    task_b = asyncio.create_task(worker("B", 0.01))
    await asyncio.gather(task_a, task_b)

    # A must fully enter+exit before B enters.
    assert order == ["A:enter", "A:exit", "B:enter", "B:exit"]
