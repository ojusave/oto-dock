"""Usage tracking and limits API endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth.providers import get_current_user, require_auth, require_admin, UserContext
from services.billing import usage_service
from storage import database as task_store

import asyncio

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SetLimitRequest(BaseModel):
    limit_type: str   # 'role_default' | 'user_override' | 'agent'
    target: str       # role name, user_sub, or agent name
    period: str       # 'weekly' | 'monthly'
    cost_limit_usd: float | None = None  # None = no limit


class DeleteLimitRequest(BaseModel):
    limit_type: str
    target: str
    period: str


# ---------------------------------------------------------------------------
# User endpoints
# ---------------------------------------------------------------------------

@router.get("/v1/usage/me")
async def get_my_usage(
    days: int = 30,
    user: UserContext = Depends(get_current_user),
):
    require_auth(user)
    summary = await asyncio.to_thread(
        usage_service.get_user_summary, user.sub, user.role, days
    )
    return summary


@router.get("/v1/usage/me/check")
async def check_my_usage(
    user: UserContext = Depends(get_current_user),
):
    require_auth(user)
    result = await asyncio.to_thread(
        usage_service.check_user_limit, user.sub, user.role
    )
    return result


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@router.get("/v1/admin/usage/overview")
async def admin_usage_overview(
    days: int = 30,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    overview = await asyncio.to_thread(usage_service.get_admin_overview, days)
    return overview


@router.get("/v1/admin/usage/limits")
async def admin_get_limits(
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    limits = await asyncio.to_thread(task_store.get_usage_limits_all)
    return {"limits": limits}


@router.put("/v1/admin/usage/limits")
async def admin_set_limit(
    req: SetLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    if req.limit_type not in ("role_default", "user_override", "agent"):
        raise HTTPException(400, "limit_type must be role_default, user_override, or agent")
    if req.period not in ("weekly", "monthly"):
        raise HTTPException(400, "period must be weekly or monthly")
    if req.limit_type == "role_default" and req.target not in ("admin", "creator", "member"):
        raise HTTPException(400, "target must be a valid role name")
    await asyncio.to_thread(
        task_store.upsert_usage_limit,
        req.limit_type, req.target, req.period, req.cost_limit_usd, user.sub,
    )
    return {"ok": True}


@router.delete("/v1/admin/usage/limits")
async def admin_delete_limit(
    req: DeleteLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    require_admin(user)
    deleted = await asyncio.to_thread(
        task_store.delete_usage_limit, req.limit_type, req.target, req.period,
    )
    if not deleted:
        raise HTTPException(404, "Limit not found")
    return {"ok": True}


@router.post("/v1/admin/usage/limits/delete")
async def admin_delete_limit_post(
    req: DeleteLimitRequest,
    user: UserContext = Depends(get_current_user),
):
    """POST variant to avoid IPS rules blocking HTTP DELETE."""
    return await admin_delete_limit(req, user)
