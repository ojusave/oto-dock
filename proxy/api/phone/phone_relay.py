"""Phone-call relay — phone-mcp's path to the phone daemon's call API.

phone-mcp used to dial the phone daemon directly (``PHONE_SERVER_URL`` +
``PHONE_API_SECRET`` in its agent_env), which required every machine that
runs an agent session to resolve and reach the daemon — remote satellites
couldn't (LAN/DNS), and the telephony secret traveled to every session
machine. These endpoints relay the daemon's call API through the proxy
instead: the MCP calls ``PROXY_URL`` with its session JWT (loopback
locally, the satellite HTTP tunnel remotely — both auto-injected), and the
proxy — the only party that needs daemon reachability — attaches
``PHONE_API_SECRET`` server-side.

Gating: a session JWT alone must not grant calling (every sandboxed
subprocess holds one). The relay resolves the token's agent and requires
``phone-mcp`` among its enabled MCPs — the same assignment gate that used
to decide who received the secret. The master key (trusted s2s) passes
without the lookup.
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response

import config
from auth.session_token import validate_session_token

logger = logging.getLogger("claude-proxy")
router = APIRouter()

# Longest daemon-side wait (mirrors phone-mcp's clamp) — the /wait long-poll
# relay reads for the caller's timeout plus slack.
_MAX_WAIT_S = 360


def _daemon_headers() -> dict:
    if config.PHONE_API_SECRET:
        return {"Authorization": f"Bearer {config.PHONE_API_SECRET}"}
    return {}


async def _require_phone_agent(authorization: str | None) -> None:
    """Bearer auth + the phone-mcp assignment gate."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    token = parts[1]
    if config.is_master_key(token):
        return
    payload = validate_session_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid API key")
    agent = payload.get("agent", "")

    from services.mcp.mcp_registry import get_agent_mcps
    manifests = await asyncio.to_thread(get_agent_mcps, agent)
    if not any(m.name == "phone-mcp" for m in manifests):
        raise HTTPException(
            status_code=403,
            detail=f"Agent {agent!r} does not have phone-mcp assigned",
        )


async def _relay(method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, read_timeout: float = 30.0) -> Response:
    """Forward one request to the phone daemon and pass the response through."""
    url = f"{config.PHONE_SERVER_URL}{path}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=read_timeout, write=10.0, pool=10.0),
        ) as client:
            resp = await client.request(
                method, url, params=params, json=json_body,
                headers=_daemon_headers(),
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Phone daemon unreachable from the proxy "
                f"({config.PHONE_SERVER_URL}): {e}"
            ),
        )
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type", "application/json"),
    )


@router.post("/v1/phone/calls")
async def relay_make_call(
    request: Request,
    authorization: str | None = Header(None),
):
    """Originate an outbound call (→ daemon ``POST /api/calls``)."""
    await _require_phone_agent(authorization)
    body = await request.json()
    return await _relay("POST", "/api/calls", json_body=body)


@router.get("/v1/phone/calls/{call_id}")
async def relay_call_status(
    call_id: str,
    authorization: str | None = Header(None),
):
    """Call status/result (→ daemon ``GET /api/calls/{id}``)."""
    await _require_phone_agent(authorization)
    return await _relay("GET", f"/api/calls/{call_id}")


@router.get("/v1/phone/calls/{call_id}/wait")
async def relay_wait_for_call(
    call_id: str,
    timeout: int = 120,
    authorization: str | None = Header(None),
):
    """Long-poll for call events (→ daemon ``GET /api/calls/{id}/wait``)."""
    await _require_phone_agent(authorization)
    timeout = max(1, min(timeout, _MAX_WAIT_S))
    return await _relay(
        "GET", f"/api/calls/{call_id}/wait",
        params={"timeout": str(timeout)},
        read_timeout=timeout + 30.0,
    )


@router.post("/v1/phone/calls/{call_id}/answer")
async def relay_answer_question(
    call_id: str,
    request: Request,
    authorization: str | None = Header(None),
):
    """Answer a mid-call [QUESTION:] (→ daemon ``POST /api/calls/{id}/answer``)."""
    await _require_phone_agent(authorization)
    body = await request.json()
    return await _relay("POST", f"/api/calls/{call_id}/answer", json_body=body)
