"""Unit tests for proxy.core.install_registry.

Focused on the registry data structure keyed by (machine_id, agent_slug):
register (refcount + participants), emit → history + per-user broadcaster,
history bounds, unregister, snapshot_inflight (connect-replay), sweeper,
fire-and-drop semantics, concurrent register/unregister.

Delivery is via a registered broadcaster (set at startup to
ws/satellite.py::push_install_event), scoped to the install's *participants*
(the user_subs that warmed this (machine, agent)) — NOT per-connection
listeners. See the module docstring for why the old listener model raced
and leaked.
"""

import asyncio
import time

import pytest

from core.remote import install_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    """Each test gets a clean registry + no broadcaster. Module-level state is
    shared with the real proxy; in tests this guarantees isolation."""
    install_registry._inflight.clear()
    install_registry.set_broadcaster(None)
    yield
    install_registry._inflight.clear()
    install_registry.set_broadcaster(None)


@pytest.mark.asyncio
async def test_register_creates_entry_and_increments_refcount():
    rec = await install_registry.register("m1", "agent-x", "user-1")
    assert rec.machine_id == "m1"
    assert rec.agent == "agent-x"
    assert rec.ref_count == 1
    assert install_registry.get("m1", "agent-x") is rec


@pytest.mark.asyncio
async def test_register_idempotent_increments_refcount():
    """Concurrent start_sessions for the same (machine, agent) share the
    same entry; each register bumps refcount.
    """
    rec1 = await install_registry.register("m1", "agent-x", "user-1")
    rec2 = await install_registry.register("m1", "agent-x", "user-1")
    assert rec1 is rec2
    assert rec1.ref_count == 2


@pytest.mark.asyncio
async def test_register_accumulates_participants():
    """Each register records the engaging user; empty user_sub adds nobody.
    A viewer + owner driving the same shared install both become recipients.
    """
    rec = await install_registry.register("m1", "agent-x", "viewer-1")
    await install_registry.register("m1", "agent-x", "owner-1")
    await install_registry.register("m1", "agent-x", "")  # phone/no-user — no add
    assert rec.participants == {"viewer-1", "owner-1"}


@pytest.mark.asyncio
async def test_register_new_participant_replays_history_to_them_only():
    """A user joining an install that already has history gets the existing
    events replayed to just them (so their bar starts at 0%, not mid-way)."""
    delivered: list[tuple[dict, list[str]]] = []

    async def _broadcast(machine_id, event, recipients):
        delivered.append((event, recipients))

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.emit("m1", "agent-x", {"type": "install_started"})
    await install_registry.emit("m1", "agent-x", {"type": "install_progress", "pct": 30})
    delivered.clear()  # drop the live deliveries to user-1

    # Second participant joins mid-install.
    await install_registry.register("m1", "agent-x", "user-2")

    assert len(delivered) == 2  # the 2 history events
    assert all(recips == ["user-2"] for _, recips in delivered)  # only the new user
    assert delivered[0][0]["type"] == "install_started"
    assert delivered[1][0]["pct"] == 30


@pytest.mark.asyncio
async def test_register_existing_participant_does_not_replay():
    """Re-registering an already-participating user (e.g. a second tab warm)
    must NOT replay history again — they already got it live."""
    delivered: list[dict] = []

    async def _broadcast(machine_id, event, recipients):
        delivered.append(event)

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.emit("m1", "agent-x", {"type": "install_started"})
    delivered.clear()

    await install_registry.register("m1", "agent-x", "user-1")  # same user, refcount 2
    assert delivered == []


@pytest.mark.asyncio
async def test_unregister_decrements_refcount_keeps_entry_alive():
    rec = await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.register("m1", "agent-x", "user-1")
    assert rec.ref_count == 2

    await install_registry.unregister("m1", "agent-x")
    # Still alive — second register holds it.
    assert install_registry.get("m1", "agent-x") is rec
    assert rec.ref_count == 1


@pytest.mark.asyncio
async def test_unregister_pops_entry_when_refcount_zero():
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.unregister("m1", "agent-x")
    assert install_registry.get("m1", "agent-x") is None


@pytest.mark.asyncio
async def test_unregister_sets_completed_event():
    rec = await install_registry.register("m1", "agent-x", "user-1")
    assert not rec.completed.is_set()
    await install_registry.unregister("m1", "agent-x")
    assert rec.completed.is_set()


@pytest.mark.asyncio
async def test_emit_appends_to_history():
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.emit("m1", "agent-x", {"type": "install_progress", "pct": 10})
    await install_registry.emit("m1", "agent-x", {"type": "install_progress", "pct": 20})

    rec = install_registry.get("m1", "agent-x")
    assert rec is not None
    assert [e["pct"] for e in rec.event_history] == [10, 20]


@pytest.mark.asyncio
async def test_emit_delivers_to_participants():
    """emit hands each event + the participant recipient list to the
    broadcaster.
    """
    delivered: list[tuple[str, dict, list[str]]] = []

    async def _broadcast(machine_id, event, recipients):
        delivered.append((machine_id, event, recipients))

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x", "viewer-1")
    await install_registry.register("m1", "agent-x", "owner-1")
    await install_registry.emit("m1", "agent-x", {"type": "install_progress", "pct": 42})

    assert len(delivered) == 1
    machine_id, event, recipients = delivered[0]
    assert machine_id == "m1"
    assert event["pct"] == 42
    assert sorted(recipients) == ["owner-1", "viewer-1"]


@pytest.mark.asyncio
async def test_emit_with_no_participants_skips_broadcaster():
    """A user-less install (phone / scheduler with no sub) still records
    history but delivers to nobody — fire-and-drop."""
    delivered: list = []

    async def _broadcast(machine_id, event, recipients):
        delivered.append(event)

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x")  # no user_sub
    await install_registry.emit("m1", "agent-x", {"type": "install_progress"})

    assert delivered == []  # broadcaster not invoked
    rec = install_registry.get("m1", "agent-x")
    assert rec is not None and len(rec.event_history) == 1  # history still kept


@pytest.mark.asyncio
async def test_emit_without_broadcaster_still_records_history():
    """No broadcaster registered (e.g. early startup) must not raise; the
    event is still kept in history for connect-time replay."""
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.emit("m1", "agent-x", {"type": "install_started"})
    rec = install_registry.get("m1", "agent-x")
    assert rec is not None
    assert rec.event_history[-1]["type"] == "install_started"


@pytest.mark.asyncio
async def test_emit_swallows_broadcaster_exception():
    """A broadcaster failure must not abort the install or propagate."""
    async def _broadcast(machine_id, event, recipients):
        raise RuntimeError("simulated delivery failure")

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x", "user-1")
    # Should not raise.
    await install_registry.emit("m1", "agent-x", {"type": "install_progress"})
    rec = install_registry.get("m1", "agent-x")
    assert rec is not None and len(rec.event_history) == 1


@pytest.mark.asyncio
async def test_event_history_bounded_at_50():
    delivered: list[dict] = []

    async def _broadcast(machine_id, event, recipients):
        delivered.append(event)

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x", "user-1")
    for i in range(60):
        await install_registry.emit("m1", "agent-x", {"type": "install_progress", "pct": i})

    # All 60 reach the broadcaster live.
    assert len(delivered) == 60
    # But history is bounded for replay.
    rec = install_registry.get("m1", "agent-x")
    assert rec is not None
    assert len(rec.event_history) == 50
    assert rec.event_history[0]["pct"] == 10
    assert rec.event_history[-1]["pct"] == 59


@pytest.mark.asyncio
async def test_emit_after_unregister_drops_silently():
    """Late emit after the entry is popped is a no-op: no exception, and the
    broadcaster is NOT invoked (nothing to deliver)."""
    delivered: list[dict] = []

    async def _broadcast(machine_id, event, recipients):
        delivered.append(event)

    install_registry.set_broadcaster(_broadcast)
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.unregister("m1", "agent-x")
    await install_registry.emit("m1", "agent-x", {"type": "install_progress"})
    assert delivered == []


@pytest.mark.asyncio
async def test_snapshot_inflight_returns_all_entries():
    """snapshot_inflight backs the dashboard's connect-time replay."""
    await install_registry.register("m1", "agent-x", "user-1")
    await install_registry.register("m2", "agent-y", "user-2")
    await install_registry.emit("m1", "agent-x", {"type": "install_started"})

    snap = install_registry.snapshot_inflight()
    machines = sorted(r.machine_id for r in snap)
    assert machines == ["m1", "m2"]
    # The returned list is a snapshot copy — clearing it doesn't mutate state.
    snap.clear()
    assert install_registry.get("m1", "agent-x") is not None


@pytest.mark.asyncio
async def test_sweeper_removes_stale_entries():
    """Entries older than _SWEEP_AGE_SECONDS get swept regardless of refcount."""
    rec1 = await install_registry.register("m1", "agent-x", "user-1")
    rec1.started_at = time.monotonic() - 700  # > 600s threshold
    # Active recent entry survives.
    await install_registry.register("m2", "agent-y", "user-2")

    removed = await install_registry.sweep_stale()
    assert removed == 1
    assert install_registry.get("m1", "agent-x") is None
    assert install_registry.get("m2", "agent-y") is not None


@pytest.mark.asyncio
async def test_concurrent_register_unregister_safe():
    """Spawn many coroutines registering/unregistering for the same key
    while a producer emits. Refcount stays consistent; no orphans."""
    await install_registry.register("m1", "agent-x", "user-1")  # initial driver

    async def _producer():
        for i in range(20):
            await install_registry.emit("m1", "agent-x", {"type": "install_progress", "pct": i})
            await asyncio.sleep(0)

    async def _driver():
        for _ in range(5):
            await install_registry.register("m1", "agent-x", "user-1")
            await asyncio.sleep(0)
            await install_registry.unregister("m1", "agent-x")

    tasks = [_producer()] + [_driver() for _ in range(10)]
    await asyncio.gather(*tasks)

    # Initial register still held by the test; ref_count should be 1
    # (each driver task did 5 register/unregister pairs that net out to 0).
    rec = install_registry.get("m1", "agent-x")
    assert rec is not None
    assert rec.ref_count == 1

    # Final cleanup of the initial driver should pop the entry.
    await install_registry.unregister("m1", "agent-x")
    assert install_registry.get("m1", "agent-x") is None
