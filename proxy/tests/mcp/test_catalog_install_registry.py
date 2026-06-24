"""Tests for core.credentials.catalog_install_registry — the admin catalog-install job table.

Covers the mutex (start), the per-MCP install lock, monotonic progress, terminal
transitions, and the sweep retention windows.
"""

import asyncio
import time

import pytest

from core.credentials import catalog_install_registry as reg


@pytest.fixture(autouse=True)
def _reset():
    reg._jobs.clear()
    reg._name_locks.clear()
    yield
    reg._jobs.clear()
    reg._name_locks.clear()


@pytest.mark.asyncio
async def test_start_is_mutex_for_running_job():
    """A second start() while a job is running returns the SAME job + False, so
    the caller knows not to spawn a second install."""
    job1, new1 = await reg.start("foo", triggered_by="admin", runtime="node", label="Foo")
    assert new1 is True
    assert job1.status == reg.STATUS_RUNNING

    job2, new2 = await reg.start("foo")
    assert new2 is False
    assert job2 is job1


@pytest.mark.asyncio
async def test_start_replaces_terminal_job():
    """A re-install after a terminal job (still in the retain window) starts
    fresh rather than joining the old finished job."""
    job1, _ = await reg.start("foo")
    await reg.finish("foo", {"status": "installed"})
    assert job1.status == reg.STATUS_DONE

    job2, new2 = await reg.start("foo")
    assert new2 is True
    assert job2 is not job1
    assert job2.status == reg.STATUS_RUNNING


def test_lock_for_same_instance_per_name():
    a1 = reg.lock_for("foo")
    a2 = reg.lock_for("foo")
    assert a1 is a2
    assert reg.lock_for("bar") is not a1


@pytest.mark.asyncio
async def test_lock_for_serializes_concurrent_acquirers():
    lock = reg.lock_for("foo")
    order: list[str] = []

    async def worker(name: str, delay: float):
        async with lock:
            order.append(f"{name}:enter")
            await asyncio.sleep(delay)
            order.append(f"{name}:exit")

    await asyncio.gather(worker("a", 0.02), worker("b", 0.0))
    # Whoever entered first fully exits before the other enters.
    assert order[0].endswith(":enter")
    assert order[1] == order[0].replace(":enter", ":exit")


@pytest.mark.asyncio
async def test_update_monotonic_and_clamped():
    await reg.start("foo")
    await reg.update("foo", phase="install", pct=40, message="installing")
    await reg.update("foo", pct=20)            # lower → must not rewind
    assert reg.get("foo").pct == 40
    await reg.update("foo", pct=150)           # over 100 → clamp
    assert reg.get("foo").pct == 100


@pytest.mark.asyncio
async def test_update_noop_after_terminal():
    await reg.start("foo")
    await reg.fail("foo", "boom")
    await reg.update("foo", pct=50, phase="install")
    j = reg.get("foo")
    assert j.status == reg.STATUS_FAILED
    assert j.phase == "failed"                 # untouched by the late update


@pytest.mark.asyncio
async def test_finish_and_fail_wire_shape():
    await reg.start("foo", runtime="docker", label="Foo")
    await reg.finish("foo", {"status": "installed", "version": "1.0.0", "install_log": "x"})
    d = reg.get("foo").to_dict()
    assert d["status"] == "done" and d["pct"] == 100 and d["error"] is None
    assert d["runtime"] == "docker" and d["label"] == "Foo"
    assert "result" not in d                   # raw install result is not on the wire

    await reg.start("bar")
    await reg.fail("bar", "x" * 5000)
    jb = reg.get("bar")
    assert jb.status == "failed"
    assert len(jb.error) <= 2000               # truncated
    assert jb.to_dict()["error"]


@pytest.mark.asyncio
async def test_sweep_drops_aged_terminal_and_stuck_running_keeps_fresh():
    await reg.start("running_fresh")           # fresh running → kept

    await reg.start("done_old")                # terminal, aged past retain → dropped
    await reg.finish("done_old")
    reg.get("done_old").finished_at = time.monotonic() - (reg._TERMINAL_RETAIN_SECONDS + 5)

    await reg.start("done_fresh")              # terminal, fresh → kept
    await reg.finish("done_fresh")

    await reg.start("running_stuck")           # running past backstop → dropped
    reg.get("running_stuck").started_at = time.monotonic() - (reg._RUNNING_MAX_SECONDS + 5)

    removed = await reg.sweep_stale()
    assert removed == 2
    assert {j.name for j in reg.snapshot()} == {"running_fresh", "done_fresh"}
