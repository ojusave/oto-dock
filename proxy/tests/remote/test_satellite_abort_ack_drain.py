"""``session_aborted`` ack drain is gated on an ARMED hard abort.

The satellite acks every ``abort`` frame with ``session_aborted``. For a HARD
abort the proxy arms the ack (arm_abort_acked) before sending and the handler
drains the session queue — events the dying CLI flushed during the kill window
must not leak into the next auto-resumed turn. A GRACEFUL codex abort reuses
the same ``abort`` wire frame but keeps its producer ALIVE to consume the
closing turn's tail (terminal turn event included); draining on its ack would
steal that terminal event and strand the producer until its timeout. Observed
live 2026-07-09 (session b998cc06): the ack lands ~100ms after the interrupt,
squarely inside the window where codex's terminal event is still in flight.
"""

import asyncio
import types

import pytest


def _fake_conn(session_queues):
    return types.SimpleNamespace(
        session_queues=session_queues,
        session_execution_paths={},
        writer_task=None,
    )


@pytest.mark.asyncio
async def test_armed_hard_abort_drains_stale_events():
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    q = asyncio.Queue(maxsize=10)
    q.put_nowait({"stale": 1})
    q.put_nowait({"stale": 2})
    cm._connections["m"] = _fake_conn({"s": q})

    cm.arm_abort_acked("m", "s")
    await cm.handle_message("m", {"type": "session_aborted", "session_id": "s"})

    assert q.empty()  # kill-window flush cleared
    assert await cm.wait_abort_acked("m", "s", timeout=0.1) is True


@pytest.mark.asyncio
async def test_graceful_abort_ack_leaves_queue_to_the_live_producer():
    from core.remote.satellite_connection import SatelliteConnectionManager

    cm = SatelliteConnectionManager()
    q = asyncio.Queue(maxsize=10)
    # The closing turn's tail — including the terminal event the kept-alive
    # producer needs to end the turn — is already queued when the ack lands.
    q.put_nowait({"type": "session_event", "tail": True})
    q.put_nowait({"type": "_turn_ended", "command_id": "cmd-1"})
    cm._connections["m"] = _fake_conn({"s": q})

    # No arm_abort_acked: graceful path.
    await cm.handle_message("m", {"type": "session_aborted", "session_id": "s"})

    assert q.qsize() == 2  # untouched — the producer consumes these
