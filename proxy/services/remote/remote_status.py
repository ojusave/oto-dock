"""Live satellite connection status — merges in-memory WS state with DB.

The DB's `remote_machines.status` column is updated on lifecycle events
(connect, disconnect, heartbeat monitor tick) but can lag reality between
ticks. The authoritative source is `SatelliteConnectionManager._connections`
plus `last_heartbeat` timestamp. This module provides helpers that merge
both so API responses reflect the current state.

State categories:
- `online` — in-memory connection present, heartbeat < 60s ago
- `stale`  — in-memory connection present, heartbeat 60-90s ago (likely dying)
- `paused` — not connected AND the deliberate-pause flag is set (tray Pause).
  Offline by intent; a friendlier label than `disconnected`. Not reachable.
- `disconnected` — not in in-memory _connections and has last_seen in DB
- `never_connected` — no row or no last_seen (machine paired but never online)
"""

import time
from typing import Any

# Heartbeat age thresholds. The satellite sends a heartbeat every 30s and the
# proxy heartbeat monitor flips DB status to `disconnected` after 90s. We use
# tighter UI thresholds so the dashboard shows `stale` before the DB catches up.
_ONLINE_MAX_AGE_S = 60.0
_STALE_MAX_AGE_S = 90.0


def get_live_machine_status(machine_id: str) -> dict[str, Any]:
    """Return the authoritative live status for a machine.

    Reads from the in-memory `SatelliteConnectionManager._connections` map
    first (source of truth for active WS); falls back to DB `last_seen` for
    machines not currently connected.

    Returns a dict:
        {
          "state": "online" | "stale" | "paused" | "disconnected"
                   | "never_connected",
          "last_heartbeat_age_s": int | None,
          "last_seen_iso": str,
          "reachable": bool,  # True iff state in {online, stale}
        }
    """
    from core.remote.satellite_connection import get_connection_manager
    from storage import remote_store

    cm = get_connection_manager()
    conn = cm.get_connection(machine_id)
    machine = remote_store.get_remote_machine(machine_id)
    last_seen = (machine or {}).get("last_seen") or ""

    if conn is not None:
        age = time.monotonic() - conn.last_heartbeat
        if age < _ONLINE_MAX_AGE_S:
            state = "online"
        elif age < _STALE_MAX_AGE_S:
            state = "stale"
        else:
            state = "disconnected"
        return {
            "state": state,
            "last_heartbeat_age_s": int(age),
            "last_seen_iso": last_seen,
            "reachable": state in ("online", "stale"),
        }

    # Not currently connected. A deliberately-paused machine reports
    # "paused" (offline by intent — friendlier than "disconnected", and the
    # admin offline evaluator already skips it). Still not reachable.
    if machine and machine.get("paused"):
        return {
            "state": "paused",
            "last_heartbeat_age_s": None,
            "last_seen_iso": last_seen,
            "reachable": False,
        }

    # Distinguish never-paired/never-online from formerly-connected via
    # DB last_seen.
    if not machine or not last_seen:
        return {
            "state": "never_connected",
            "last_heartbeat_age_s": None,
            "last_seen_iso": last_seen,
            "reachable": False,
        }

    return {
        "state": "disconnected",
        "last_heartbeat_age_s": None,
        "last_seen_iso": last_seen,
        "reachable": False,
    }


def is_reachable(machine_id: str) -> bool:
    """Shortcut: True iff the machine can accept commands right now."""
    return get_live_machine_status(machine_id)["reachable"]
