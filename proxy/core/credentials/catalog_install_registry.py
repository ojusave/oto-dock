"""In-flight registry for ADMIN community-catalog MCP installs.

Distinct from :mod:`core.remote.install_registry`, which tracks *satellite*
session-warmup MCP sync keyed by ``(machine_id, agent)``. This registry tracks
an admin clicking **Install** on a community MCP in the dashboard Browse drawer:
a server-side install that runs on the proxy (T1 bare-metal) or via the
socket-proxy compose (T2), keyed by MCP **name**.

Why a registry at all? The install endpoint is now a background job — the POST
returns ``202`` immediately and the work runs in an ``asyncio.create_task`` — so
progress has to live somewhere the dashboard can read. The Browse drawer polls
``GET /v1/admin/community/mcps/installs`` every ~1.5s while it's open and renders
a per-MCP progress bar from these jobs. Because the registry is keyed by MCP
name (not by any client/connection), a tab that reopens mid-install simply
re-polls and sees the live state — no client-side rehydration.

Two locks, two roles:

- ``_lock`` guards the ``_jobs`` table (the job dict) for atomic
  create-or-join / update / terminal transitions.
- ``lock_for(name)`` returns a per-MCP-name lock that serializes the actual
  filesystem install pipeline. Both the direct-install background task AND
  :func:`services.community.community_installer.approve_request` acquire it, so a direct
  install and a manager-request approval of the *same* MCP can never run the
  copy/replace/dependency-install steps concurrently and corrupt the install
  dir (target_dir / its ``.bak`` backup).

All times are :func:`time.monotonic` (wall-clock is unavailable to and
irrelevant for this — only elapsed/age matters).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("catalog-install-registry")

# Keep a terminal job around this long after it finishes so the dashboard's
# ~1.5s poll reliably catches the done/failed state — generous enough to survive
# a backgrounded tab whose timers the browser throttles toward ~once/minute.
_TERMINAL_RETAIN_SECONDS = 120.0

# Hard backstop: a "running" job older than this is presumed dead (install
# subprocess hung, or the task was lost) and is swept so it stops blocking the
# per-name lock and showing a stuck bar. Above the 900s compose-pull ceiling
# plus margin for the surrounding fetch/copy/start work.
_RUNNING_MAX_SECONDS = 1800.0

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


@dataclass
class InstallJob:
    """One admin catalog-install operation for a single MCP."""

    name: str
    triggered_by: str = ""          # admin sub that clicked Install (audit/notify)
    runtime: str = ""               # "node" | "python" | "docker" (display)
    label: str = ""                 # human label (display)
    status: str = STATUS_RUNNING
    phase: str = "queued"           # fetch|prepare|install|image|start|finalize|done|failed
    pct: int = 0
    message: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    result: dict | None = None      # install_from_extracted_folder return (success)
    error: str | None = None        # rollback reason (failure)

    def to_dict(self) -> dict:
        """Wire shape for the poll endpoint. ``result`` is intentionally omitted
        — the dashboard re-reads the catalog list for installed state, and the
        raw install log can be large and isn't needed to render the bar."""
        ref = self.finished_at if self.finished_at is not None else time.monotonic()
        return {
            "name": self.name,
            "label": self.label,
            "runtime": self.runtime,
            "status": self.status,
            "phase": self.phase,
            "pct": self.pct,
            "message": self.message,
            "error": self.error,
            "elapsed_s": round(ref - self.started_at, 1),
        }


_jobs: dict[str, InstallJob] = {}
_lock = asyncio.Lock()

# Lazy per-MCP-name install locks — created on first request, never freed (one
# tiny asyncio.Lock per distinct MCP name the platform ever installs; bounded
# and cheap). Same pattern as ``core.credentials.credential_locks.get_lock``: a synchronous
# function with no ``await`` between the get and the create, so it runs to
# completion without yielding the event loop and is race-free on the single
# asyncio thread.
_name_locks: dict[str, asyncio.Lock] = {}


def lock_for(name: str) -> asyncio.Lock:
    """Return the per-MCP-name install lock, creating it on first request.

    Serializes the filesystem install pipeline for one MCP across every caller
    (the direct-install background task + ``approve_request``), so two installs
    of the same MCP can't race on its target dir / ``.bak`` backup.
    """
    lock = _name_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _name_locks[name] = lock
    return lock


async def start(
    name: str, *, triggered_by: str = "", runtime: str = "", label: str = "",
) -> tuple[InstallJob, bool]:
    """Begin a catalog install for ``name``. Atomic create-or-join.

    Returns ``(job, is_new)``. If a *running* job already exists for this MCP
    (a double-click, or a second admin), returns the existing job and
    ``is_new=False`` — the caller must NOT spawn a second install. A *terminal*
    job from a prior install (still inside the retain window) is replaced with a
    fresh running job so a re-install after a failure shows fresh progress.
    """
    async with _lock:
        existing = _jobs.get(name)
        if existing is not None and existing.status == STATUS_RUNNING:
            return existing, False
        job = InstallJob(
            name=name, triggered_by=triggered_by, runtime=runtime, label=label,
        )
        _jobs[name] = job
        return job, True


async def update(
    name: str, *, phase: str | None = None, pct: int | None = None,
    message: str | None = None,
) -> None:
    """Update a running job's progress. No-op if the job is gone or terminal.

    ``pct`` is clamped to [0, 100] and forced monotonic non-decreasing so a
    late lower-pct event from a sub-phase never rewinds the bar.
    """
    async with _lock:
        job = _jobs.get(name)
        if job is None or job.status != STATUS_RUNNING:
            return
        if phase is not None:
            job.phase = phase
        if pct is not None:
            job.pct = max(job.pct, min(100, int(pct)))
        if message is not None:
            job.message = message


async def finish(name: str, result: dict | None = None) -> None:
    """Mark a job done (100%). Safe if the entry is already gone."""
    async with _lock:
        job = _jobs.get(name)
        if job is None:
            return
        job.status = STATUS_DONE
        job.phase = "done"
        job.pct = 100
        job.message = "installed"
        job.result = result
        job.finished_at = time.monotonic()


async def fail(name: str, error: str) -> None:
    """Mark a job failed with the rollback reason. Safe if already gone."""
    async with _lock:
        job = _jobs.get(name)
        if job is None:
            return
        job.status = STATUS_FAILED
        job.phase = "failed"
        job.error = str(error)[:2000]
        job.message = "install failed"
        job.finished_at = time.monotonic()


def get(name: str) -> InstallJob | None:
    """Race-tolerant synchronous peek at one job."""
    return _jobs.get(name)


def snapshot() -> list[InstallJob]:
    """All jobs (running + recently-terminal). Used by the poll endpoint."""
    return list(_jobs.values())


async def sweep_stale() -> int:
    """Drop terminal jobs past the retain window + runaway running jobs.

    Called periodically from ``proxy/app.py``'s registry sweep loop. Returns the
    count removed (for logging).
    """
    now = time.monotonic()
    removed = 0
    async with _lock:
        for k in list(_jobs.keys()):
            job = _jobs[k]
            if job.status == STATUS_RUNNING:
                if now - job.started_at > _RUNNING_MAX_SECONDS:
                    _jobs.pop(k, None)
                    removed += 1
            else:
                ref = job.finished_at if job.finished_at is not None else job.started_at
                if now - ref > _TERMINAL_RETAIN_SECONDS:
                    _jobs.pop(k, None)
                    removed += 1
    if removed:
        logger.info("catalog_install_registry: swept %d stale job(s)", removed)
    return removed
