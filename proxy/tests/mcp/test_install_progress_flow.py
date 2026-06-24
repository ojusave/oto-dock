"""Integration tests for the install progress event flow.

Verifies that mcp_sync.sync_mcps_for_session emits its plan via the
``plan_cb`` callback (the registry-agnostic surface introduced when
install_registry replaced chat_id routing), and that the same shape
fans out through install_registry to the registered per-user broadcaster.

Does not spawn a real satellite or CLI — uses module-level patching to
keep tests fast and deterministic.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.remote import install_registry


@pytest.fixture(autouse=True)
def _reset_registry():
    install_registry._inflight.clear()
    install_registry.set_broadcaster(None)
    yield
    install_registry._inflight.clear()
    install_registry.set_broadcaster(None)


@pytest.mark.asyncio
async def test_progress_event_shape_fans_out_through_broadcaster():
    """The closure in remote_execution.start_session forwards satellite
    progress events to install_registry, which hands them to the per-user
    broadcaster. Simulate the closure + broadcaster pattern.
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    sat_event = {
        "mcp": "file-tools",
        "phase": "pip",
        "pct": 50,
        "message": "pip install file-tools",
    }
    await install_registry.emit(machine_id, agent, {
        "type": "install_progress",
        "machine_id": machine_id,
        "agent": agent,
        "mcp": sat_event["mcp"],
        "phase": sat_event["phase"],
        "pct": sat_event["pct"],
        "message": sat_event["message"],
    })

    assert len(received) == 1
    assert received[0]["type"] == "install_progress"
    assert received[0]["machine_id"] == machine_id
    assert received[0]["agent"] == agent
    assert received[0]["mcp"] == "file-tools"
    assert received[0]["pct"] == 50


@pytest.mark.asyncio
async def test_mcp_install_failed_event_shape():
    """Soft-fail surface: each excluded MCP gets a mcp_install_failed event
    keyed by (machine_id, agent) — not chat_id.
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    await install_registry.emit(machine_id, agent, {
        "type": "mcp_install_failed",
        "machine_id": machine_id,
        "agent": agent,
        "mcp": "unifi-network",
        "error": "Python>=3.13 required",
    })

    assert len(received) == 1
    assert received[0]["type"] == "mcp_install_failed"
    assert received[0]["mcp"] == "unifi-network"
    assert "Python" in received[0]["error"]


@pytest.mark.asyncio
async def test_connect_replays_history_then_live_broadcast():
    """A tab that connects mid-install replays the bounded event history
    (what the dashboard pushes from snapshot_inflight on connect) so the UI
    catches up; subsequent emits reach it live via the broadcaster.
    """
    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    # Live events fire before any tab is watching — they accumulate in
    # history and (if a broadcaster is set) get delivered live.
    await install_registry.emit(machine_id, agent, {"type": "install_started", "machine_id": machine_id, "agent": agent})
    await install_registry.emit(machine_id, agent, {"type": "install_progress", "machine_id": machine_id, "agent": agent, "mcp": "a", "pct": 10})
    await install_registry.emit(machine_id, agent, {"type": "install_progress", "machine_id": machine_id, "agent": agent, "mcp": "a", "pct": 50})

    # A tab connects: the dashboard reads snapshot_inflight and replays the
    # in-flight install's history into that connection.
    replayed: list[dict] = []
    snap = install_registry.snapshot_inflight()
    rec = next(r for r in snap if r.machine_id == machine_id and r.agent == agent)
    for past_ev in list(rec.event_history):
        replayed.append(past_ev)

    assert len(replayed) == 3
    assert replayed[0]["type"] == "install_started"
    assert replayed[-1]["pct"] == 50

    # From now on the connection is registered with notification_manager, so
    # live events reach it via the broadcaster.
    live: list[dict] = []

    async def _broadcast(mid, ev, recipients):
        live.append(ev)

    install_registry.set_broadcaster(_broadcast)
    await install_registry.emit(machine_id, agent, {"type": "install_done", "machine_id": machine_id, "agent": agent})
    assert live[-1]["type"] == "install_done"


@pytest.mark.asyncio
async def test_install_failed_emits_through_registry():
    """Whole-install failure is emitted through the registry so it reaches
    the broadcaster (and the bounded history for connect replay).
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    await install_registry.emit(machine_id, agent, {
        "type": "install_failed",
        "machine_id": machine_id,
        "agent": agent,
        "error": "sync_mcps timed out",
    })

    assert received[-1]["type"] == "install_failed"
    assert "timed out" in received[-1]["error"]


@pytest.mark.asyncio
async def test_install_heartbeat_when_quiet():
    """During quiet install, a heartbeat fires every 15s of registry
    silence. Simulate by manually emitting heartbeats.
    """
    received: list[dict] = []

    async def _broadcast(machine_id, ev, recipients):
        received.append(ev)

    install_registry.set_broadcaster(_broadcast)

    machine_id = "machine-1"
    agent = "agent-x"
    await install_registry.register(machine_id, agent, "user-1")

    await install_registry.emit(machine_id, agent, {"type": "install_heartbeat", "machine_id": machine_id, "agent": agent})
    await install_registry.emit(machine_id, agent, {"type": "install_heartbeat", "machine_id": machine_id, "agent": agent})

    assert sum(1 for ev in received if ev["type"] == "install_heartbeat") == 2


@pytest.mark.asyncio
async def test_mcp_sync_invokes_plan_cb(monkeypatch):
    """mcp_sync.sync_mcps_for_session calls plan_cb once the diff is
    computed. Verified by mocking the satellite connection manager.
    """
    from services.mcp import mcp_sync

    plan_events: list[dict] = []

    async def _plan_cb(ev):
        plan_events.append(ev)

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())
    fake_cm.send_command = AsyncMock(return_value={"results": {}})
    fake_cm.register_install_progress = MagicMock()
    fake_cm.unregister_install_progress = MagicMock()

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    async def _fake_fetch(cm, machine_id):
        return {}

    fake_manifest = MagicMock()
    fake_manifest.server.runtime = "node"
    fake_manifest.server.source = "npm:fake@1.0"
    fake_manifest.category = "community"
    fake_manifest.system_requirements.debian = []
    fake_manifest.system_requirements.ubuntu = []
    fake_manifest.system_requirements.rhel = []
    fake_manifest.system_requirements.arch = []
    fake_manifest.system_requirements.macos_brew = []
    fake_manifest.system_requirements.node_min = ""
    fake_manifest.system_requirements.notes = ""

    fake_tarball = MagicMock()
    fake_tarball.tarball_b64 = ""
    fake_tarball.version_hash = "hash1"

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: fake_manifest if name == "file-tools" else None,
    )
    monkeypatch.setattr(
        "services.mcp.mcp_tarball.build_tarball",
        lambda name: fake_tarball,
    )
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager",
        lambda: fake_cm,
    )
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer",
        lambda: fake_layer,
    )
    monkeypatch.setattr(
        mcp_sync, "_manifest_to_dict",
        lambda m: {"name": "file-tools"},
    )

    await mcp_sync.sync_mcps_for_session(
        "machine-1", "session-1", ["file-tools"], plan_cb=_plan_cb,
    )

    assert len(plan_events) == 1
    assert plan_events[0]["mcps_to_install"] == ["file-tools"]
    assert plan_events[0]["mcps_to_update"] == []


@pytest.mark.asyncio
async def test_mcp_sync_skips_plan_cb_when_no_install_needed(monkeypatch):
    """When the satellite is already in sync, plan_cb is not invoked."""
    from services.mcp import mcp_sync

    plan_events: list[dict] = []

    async def _plan_cb(ev):
        plan_events.append(ev)

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    # Satellite already has file-tools healthy with matching version_hash.
    async def _fake_fetch(cm, machine_id):
        return {"file-tools": {"version_hash": "hash1", "healthy": True}}

    fake_manifest = MagicMock()
    fake_manifest.server.runtime = "node"
    fake_manifest.mcp_dir = "/fake/dir"

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: fake_manifest if name == "file-tools" else None,
    )
    monkeypatch.setattr(
        "services.mcp.mcp_installer.compute_version_hash",
        lambda d: "hash1",
    )
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager",
        lambda: fake_cm,
    )
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer",
        lambda: fake_layer,
    )

    result = await mcp_sync.sync_mcps_for_session(
        "machine-1", "session-1", ["file-tools"], plan_cb=_plan_cb,
    )

    assert result.ok
    # Nothing to install/update/remove → plan_cb never fires.
    assert plan_events == []


@pytest.mark.asyncio
async def test_sync_result_captures_warmup_failures(monkeypatch):
    """The satellite ack reports a per-MCP pre-warm boot result in
    ``results[name]["warmup"]``. ``sync_mcps_for_session`` folds
    ``"warn:<reason>"`` into ``SyncResult.warmup_failed`` — advisory only,
    so the MCP is still counted as installed and is NOT excluded."""
    from services.mcp import mcp_sync

    fake_cm = MagicMock()
    fake_cm.is_connected = MagicMock(return_value=True)
    fake_cm.get_install_lock = MagicMock(return_value=_async_ctx())
    fake_cm.register_install_progress = MagicMock()
    fake_cm.unregister_install_progress = MagicMock()
    # Two MCPs installed ok; one answered initialize, one timed out.
    fake_cm.send_command = AsyncMock(return_value={"results": {
        "file-tools": {"status": "ok", "version_hash": "h1", "warmup": "ok"},
        "google-maps": {"status": "ok", "version_hash": "h2", "warmup": "warn:timeout"},
    }})

    fake_layer = MagicMock()
    fake_layer._sessions = {}

    async def _fake_fetch(cm, machine_id):
        return {}

    def _mk_manifest():
        m = MagicMock()
        m.server.runtime = "python"
        m.server.source = "pypi:x@1"
        m.category = "custom"
        for f in ("debian", "ubuntu", "rhel", "arch", "macos_brew"):
            setattr(m.system_requirements, f, [])
        m.system_requirements.node_min = ""
        m.system_requirements.notes = ""
        return m

    fake_tb = MagicMock()
    fake_tb.tarball_b64 = ""
    fake_tb.version_hash = "h"

    monkeypatch.setattr(mcp_sync, "_fetch_satellite_state", _fake_fetch)
    monkeypatch.setattr(
        "services.mcp.mcp_registry.get_manifest",
        lambda name: _mk_manifest() if name in ("file-tools", "google-maps") else None,
    )
    monkeypatch.setattr("services.mcp.mcp_tarball.build_tarball", lambda name: fake_tb)
    monkeypatch.setattr(
        "core.remote.satellite_connection.get_connection_manager", lambda: fake_cm)
    monkeypatch.setattr(
        "core.remote.remote_execution.get_remote_layer", lambda: fake_layer)
    monkeypatch.setattr(
        mcp_sync, "_manifest_to_dict", lambda m: {"name": "x"})

    result = await mcp_sync.sync_mcps_for_session(
        "machine-1", "session-1", ["file-tools", "google-maps"],
    )

    assert set(result.installed) == {"file-tools", "google-maps"}
    assert result.warmup_failed == {"google-maps": "timeout"}
    # Advisory: a warm-up failure does NOT exclude the MCP from the session.
    assert result.excluded_names == set()


class _async_ctx:
    """Tiny async context manager stub that no-ops on enter/exit."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False
