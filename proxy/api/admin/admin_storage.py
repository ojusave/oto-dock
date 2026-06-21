"""Admin storage & retention endpoints (Setup → Storage & Retention card).

The settings themselves (session_retention_enabled / session_retention_days)
ride the shared platform-settings GET/PUT in api/auth/auth.py; this module hosts
the action + readout endpoints backed by services/infra/retention.py.
"""

import asyncio

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from auth.providers import get_current_user, require_admin, UserContext
from services.infra import retention

router = APIRouter()


class RetentionRunRequest(BaseModel):
    dry_run: bool = False


@router.post("/v1/admin/retention/run-now")
async def admin_retention_run_now(
    req: RetentionRunRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Run the full retention sweep immediately (all passes; the aged-chats
    pass still honors the enabled toggle). dry_run reports what WOULD be
    deleted without touching anything."""
    require_admin(user)
    return await retention.run_sweep(dry_run=req.dry_run)


@router.get("/v1/admin/storage/usage")
async def admin_storage_usage(
    user: UserContext | None = Depends(get_current_user),
):
    """Disk-usage breakdown for the admin card (agents tree, session files,
    codex junk, recover-bin, proxy sessions dir, logs) + retention status."""
    require_admin(user)
    return await asyncio.to_thread(retention.compute_storage_usage)
