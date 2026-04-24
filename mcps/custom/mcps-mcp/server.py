"""MCPs-MCP — agents browse + request MCPs from chat.

Stdio MCP server that lets an agent inspect the platform's MCP state
(``list_enabled_mcps``, ``list_available_mcps``) and the community
catalog (``list_community_mcps``), and act on that state via:

- ``request_mcp_install`` / ``request_mcp_access`` (smart-routed: direct
  enable if the MCP is already visible to this agent; admin queue
  otherwise)
- ``disable_mcp_for_agent`` (manager + admin user-scope only — agent-scope
  is gated out to avoid scheduled tasks turning off MCPs they'll need)
- ``get_request_status`` / ``cancel_my_request`` (request lifecycle helpers)

Admin-only power operations (platform-wide install without an agent
target; cross-agent enable) intentionally aren't exposed as tools — they
live in the dashboard's Browse Community drawer + agent MCPs tab. Keeping
mcps-mcp focused on the chat-level "make this work on my agent" flow
avoids the two-step admin chain that breaks down on ``explicit``-mode
MCPs needing instance config the tool layer can't provide.

Permission gating is resolved ONCE at module load using the auto-injected
``OTO_*`` env var set (``OTO_ROLE``, ``OTO_SCOPE``). Disallowed sessions
(viewers) end up with an empty tool list — the MCP starts cleanly but exposes
nothing, which is friendlier to the MCP launcher than ``sys.exit(0)`` would be
(no spurious failure logs).

All API calls use ``PROXY_URL`` + ``PROXY_API_KEY`` (auto-injected,
session-scoped JWT) so the platform applies the calling user's role to
every endpoint. The MCP's local matrix is defense-in-depth on top of
that — the platform is still the authoritative gate.

Env vars (auto-injected by ``core/oto_env.py`` + ``env_builder.py``):
  OTO_AGENT_NAME       — agent slug (used for ``/v1/agents/{slug}/...``)
  OTO_ROLE             — ``viewer``/``manager``/``admin``/``""`` (agent-scope)
  OTO_SCOPE            — ``user`` or ``agent``
  OTO_SESSION_ID       — session id (currently unused; future correlation)
  PROXY_URL            — proxy base URL
  PROXY_API_KEY        — session-scoped JWT (not the master key)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import TextContent, Tool


# ---------------------------------------------------------------------------
# Env-derived permission matrix
# ---------------------------------------------------------------------------

AGENT_NAME = os.environ.get("OTO_AGENT_NAME", "")
ROLE = os.environ.get("OTO_ROLE", "")
SCOPE = os.environ.get("OTO_SCOPE", "")

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8400").rstrip("/")
API_KEY = os.environ.get("PROXY_API_KEY", "")


def _resolve_tool_set() -> set[str]:
    """Return the set of tool names this session is allowed to call.

    The matrix:

    - ``scope=user`` × ``role=viewer`` → empty (viewers don't manage MCPs).
    - ``scope=user`` × ``role=manager`` → read + request tools.
    - ``scope=user`` × ``role=admin`` → all tools (read + request + admin).
    - ``scope=agent`` (task / phone / trigger / Shared-only agent) →
      read-only (no requests — agent-scope can't act on behalf of a user).
    """
    if SCOPE == "user" and ROLE == "viewer":
        return set()

    read = {"list_enabled_mcps", "list_available_mcps", "list_community_mcps"}
    if SCOPE == "agent":
        return read

    # Admin sessions get the same tool set as managers — power operations
    # (platform-wide install without an agent target; cross-agent enable)
    # live in the dashboard's Browse Community drawer + agent MCPs tab.
    # mcps-mcp stays focused on the agent's chat-level needs.
    manager = read | {
        "request_mcp_install",
        "request_mcp_access",
        "disable_mcp_for_agent",
        "get_request_status",
        "cancel_my_request",
    }
    if SCOPE == "user" and ROLE in ("manager", "admin"):
        return manager
    return set()


ENABLED_TOOLS = _resolve_tool_set()

server = Server("mcps-mcp")
_client = httpx.AsyncClient(timeout=10.0)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class _ApiError(RuntimeError):
    """Wrapped HTTP error from the proxy. Message is shown to the LLM verbatim."""


def _headers() -> dict[str, str]:
    h = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    if AGENT_NAME:
        h["X-Agent-Name"] = AGENT_NAME
    return h


async def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call the platform with one retry on 5xx (1s backoff)."""
    url = f"{PROXY_URL}{path}"
    for attempt in (0, 1):
        try:
            r = await _client.request(
                method, url,
                params=params,
                json=body,
                headers=_headers(),
            )
        except httpx.HTTPError as exc:
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            raise _ApiError(f"network: {exc}") from exc

        if 500 <= r.status_code < 600 and attempt == 0:
            await asyncio.sleep(1.0)
            continue

        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise _ApiError(f"HTTP {r.status_code}: {detail}")

        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return r.text

    raise _ApiError("retry exhausted")  # pragma: no cover


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    return await _request("GET", path, params=params)


async def _post(path: str, body: dict[str, Any] | None = None) -> Any:
    return await _request("POST", path, body=body or {})


async def _put(path: str, body: dict[str, Any] | None = None) -> Any:
    return await _request("PUT", path, body=body or {})


# ---------------------------------------------------------------------------
# 60s in-process cache for catalog reads
# ---------------------------------------------------------------------------

_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_put(key: str, value: Any) -> None:
    _cache[key] = (time.monotonic(), value)


def _cache_invalidate() -> None:
    _cache.clear()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "_(empty)_"
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join(["---"] * len(headers)) + " |"
    body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
    return f"{head}\n{sep}\n{body}"


def _truncate(text: str, n: int = 80) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _install_status_for(entry: dict) -> str:
    """One of: not_installed | installed_not_enabled | enabled_for_agent."""
    if not entry.get("installed"):
        return "not_installed"
    enabled_for = entry.get("enabled_for_agents") or []
    if AGENT_NAME and AGENT_NAME in enabled_for:
        return "enabled_for_agent"
    return "installed_not_enabled"


# ---------------------------------------------------------------------------
# Tool catalog (gated by ENABLED_TOOLS)
# ---------------------------------------------------------------------------


_ALL_TOOLS: dict[str, Tool] = {
    "list_enabled_mcps": Tool(
        name="list_enabled_mcps",
        description=(
            "List MCPs currently enabled for this agent. Returns a markdown "
            "table (name | description | category)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    "list_available_mcps": Tool(
        name="list_available_mcps",
        description=(
            "List MCPs installed on the platform that are NOT yet enabled "
            "for this agent. Useful before calling request_mcp_access. "
            "Returns a markdown table (name | description | category)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    "list_community_mcps": Tool(
        name="list_community_mcps",
        description=(
            "Browse the community MCP catalog (the OtoDock-curated repo). "
            "Filter by category or substring search. Returns a markdown "
            "table (name | description | category | tags | install_status). "
            "install_status ∈ {not_installed, installed_not_enabled, "
            "enabled_for_agent}. Use request_mcp_install for not_installed "
            "and request_mcp_access for installed_not_enabled."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Optional category filter (substring match).",
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Optional case-insensitive substring search over "
                        "name + description + tags."
                    ),
                },
            },
        },
    ),
    "request_mcp_install": Tool(
        name="request_mcp_install",
        description=(
            "Get a community MCP enabled for this agent. Smart-routes: if "
            "the MCP is already installed and available to this agent, it "
            "enables directly (no admin involvement). Only if the MCP "
            "isn't installed yet (or needs admin instance configuration) "
            "does this fall through to a request the admin must approve. "
            "Use this when the MCP shows install_status=not_installed in "
            "the community catalog. The admin sees your reason when a "
            "request is actually filed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mcp_name": {
                    "type": "string",
                    "description": "MCP slug (see list_community_mcps).",
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Short justification shown to the admin (~100 chars). "
                        "Compose from the user's request — e.g. 'user asked to "
                        "search nearby restaurants' for google-maps."
                    ),
                },
            },
            "required": ["mcp_name", "reason"],
        },
    ),
    "request_mcp_access": Tool(
        name="request_mcp_access",
        description=(
            "Get an already-installed MCP enabled for this agent. Smart-"
            "routes: if the MCP is available to this agent (auto-mode, or "
            "explicit-mode with an instance authorizing this agent), it "
            "enables directly — no admin approval needed, no request "
            "filed. Only when the MCP isn't visible to this agent (e.g. "
            "explicit-mode without instance config) does it fall through "
            "to a request the admin must approve. Use this when the MCP "
            "shows install_status=installed_not_enabled. Reason is shown "
            "to the admin only when a request is actually filed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mcp_name": {
                    "type": "string",
                    "description": "MCP slug (see list_available_mcps).",
                },
                "reason": {
                    "type": "string",
                    "description": "Short justification shown to the admin (~100 chars).",
                },
            },
            "required": ["mcp_name", "reason"],
        },
    ),
    "disable_mcp_for_agent": Tool(
        name="disable_mcp_for_agent",
        description=(
            "Remove an MCP from this agent's enabled set. Manager-level "
            "toggle — same as unticking the MCP on the agent's MCPs tab "
            "in the dashboard. No admin involvement. No-ops with a "
            "friendly message if the MCP wasn't enabled in the first "
            "place. Disables are platform-wide for this agent (affects "
            "future sessions too), so think before calling — for "
            "short-term 'don't use this MCP right now' the manager "
            "should just instruct in chat instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "mcp_name": {
                    "type": "string",
                    "description": "MCP slug (see list_enabled_mcps).",
                },
            },
            "required": ["mcp_name"],
        },
    ),
    "get_request_status": Tool(
        name="get_request_status",
        description=(
            "Check the status of an MCP install/access request you created "
            "for this agent. Returns the current state (pending/approved/"
            "installing/installed/rejected/cancelled/install_failed) and any "
            "admin note."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "integer"},
            },
            "required": ["request_id"],
        },
    ),
    "cancel_my_request": Tool(
        name="cancel_my_request",
        description=(
            "Cancel an open request you created. Only the requester (or an "
            "admin via the dashboard) can cancel. Approved/rejected/installed "
            "requests are terminal and cannot be cancelled."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "integer"},
            },
            "required": ["request_id"],
        },
    ),
}


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [tool for name, tool in _ALL_TOOLS.items() if name in ENABLED_TOOLS]


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _handle_list_enabled_mcps(_args: dict) -> str:
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    data = await _get(f"/v1/agents/{AGENT_NAME}/mcps")
    rows = [m for m in (data or {}).get("mcps", []) if m.get("enabled")]
    if not rows:
        return f"No MCPs enabled for **{AGENT_NAME}**."
    body = _md_table(
        ["name", "category", "description"],
        [
            [m.get("name", ""), m.get("category", ""), _truncate(m.get("description", ""))]
            for m in sorted(rows, key=lambda r: (r.get("category") != "core", r.get("name", "")))
        ],
    )
    return f"MCPs enabled for **{AGENT_NAME}** ({len(rows)}):\n\n{body}"


async def _handle_list_available_mcps(_args: dict) -> str:
    """Installed-on-platform but NOT enabled for this agent.

    Sourced from the community catalog with ``?agent=`` scoping — that's the
    same endpoint the Browse drawer uses, and it already tags
    ``installed`` + ``enabled_for_agents`` per row.
    """
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    cache_key = f"catalog:{AGENT_NAME}"
    cached = _cache_get(cache_key)
    if cached is None:
        data = await _get("/v1/community/mcps", params={"agent": AGENT_NAME})
        _cache_put(cache_key, data)
    else:
        data = cached

    rows = []
    for m in (data or {}).get("mcps", []):
        if not m.get("installed"):
            continue
        if AGENT_NAME in (m.get("enabled_for_agents") or []):
            continue
        rows.append(m)

    if not rows:
        return (
            "No community MCPs are installed on the platform but missing "
            f"from **{AGENT_NAME}**."
        )
    body = _md_table(
        ["name", "category", "description"],
        [
            [m.get("name", ""), m.get("category", ""), _truncate(m.get("description", ""))]
            for m in sorted(rows, key=lambda r: r.get("name", ""))
        ],
    )
    return (
        f"Installed-but-not-enabled MCPs ({len(rows)}). Call "
        f"`request_mcp_access` to ask the admin to enable one:\n\n{body}"
    )


async def _handle_list_community_mcps(args: dict) -> str:
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"

    category = (args.get("category") or "").strip().lower()
    search = (args.get("search") or "").strip().lower()

    cache_key = f"catalog:{AGENT_NAME}"
    cached = _cache_get(cache_key)
    if cached is None:
        data = await _get("/v1/community/mcps", params={"agent": AGENT_NAME})
        _cache_put(cache_key, data)
    else:
        data = cached

    entries = (data or {}).get("mcps", [])
    if category:
        entries = [e for e in entries if category in (e.get("category") or "").lower()]
    if search:
        def _hit(e: dict) -> bool:
            hay = " ".join([
                e.get("name") or "",
                e.get("description") or "",
                " ".join(e.get("tags") or []),
            ]).lower()
            return search in hay
        entries = [e for e in entries if _hit(e)]

    if not entries:
        return "No community MCPs match those filters."

    rows = []
    for e in sorted(entries, key=lambda x: x.get("name", "")):
        rows.append([
            e.get("name", ""),
            e.get("category", ""),
            _truncate(e.get("description", "")),
            ", ".join(e.get("tags") or []) or "—",
            _install_status_for(e),
        ])
    body = _md_table(
        ["name", "category", "description", "tags", "install_status"],
        rows,
    )
    return f"Community catalog matches ({len(rows)}):\n\n{body}"


async def _try_enable_directly(mcp_name: str) -> tuple[str, str | None]:
    """If the MCP is visible to this agent and not yet enabled, enable it
    directly via PUT — no admin involvement needed.

    The platform endpoint ``PUT /v1/agents/{slug}/mcps`` is open to managers
    (any user with manage rights on the agent), and ``GET /v1/agents/{slug}/mcps``
    returns the visibility set. So when the MCP is already installed AND
    visible (auto-mode, or explicit-mode with an instance authorizing this
    agent), the manager has full perm to toggle it on themselves.

    Returns ``(status, message)`` where status is one of:

    - ``"enabled"`` — successfully enabled via PUT; message is the confirmation.
    - ``"already_enabled"`` — already in the enabled set; message is the no-op note.
    - ``"not_visible"`` — MCP isn't visible to this agent (not installed OR
      explicit-mode without instance auth); ``message=None``, caller should
      proceed with the request flow.
    - ``"fail"`` — API error during the visibility check or PUT; message
      is the error string ready for the LLM.
    """
    try:
        data = await _get(f"/v1/agents/{AGENT_NAME}/mcps")
    except _ApiError as exc:
        return ("fail", f"❌ Error: {exc}")

    visible = (data or {}).get("mcps", [])
    entry = next((m for m in visible if m.get("name") == mcp_name), None)
    if entry is None:
        return ("not_visible", None)
    if entry.get("enabled"):
        return (
            "already_enabled",
            f"ℹ️  `{mcp_name}` is already enabled for **{AGENT_NAME}**.",
        )

    # Visible + not enabled → manager (and therefore the agent on the
    # manager's behalf) can toggle directly. Full-replace via PUT keeps
    # the endpoint contract simple.
    enabled = [m.get("name") for m in visible if m.get("enabled")]
    enabled.append(mcp_name)
    try:
        await _put(f"/v1/agents/{AGENT_NAME}/mcps", {"mcps": enabled})
    except _ApiError as exc:
        return ("fail", f"❌ Error: {exc}")
    _cache_invalidate()
    return (
        "enabled",
        f"✅ `{mcp_name}` enabled for **{AGENT_NAME}** directly "
        f"(no admin approval needed — the MCP was already installed and "
        f"available to this agent).",
    )


async def _create_request(mcp_name: str, reason: str) -> str:
    """Smart-route between direct enable and admin request.

    Flow:

    1. If the MCP is already visible-but-not-enabled → enable directly via
       PUT (manager-level perm; no admin involvement). Reason is ignored
       because there's no request to attach it to.
    2. If the MCP is visible-and-enabled → 409-equivalent no-op.
    3. Otherwise → POST a request to the admin queue.

    ``reason`` is required at the tool schema layer (LLMs compose
    context-aware reasons cheaply); the REST API treats it as optional so
    dashboard-originated requests can be empty without server gymnastics.
    """
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    if not mcp_name:
        return "❌ Error: mcp_name is required"
    if not reason:
        return "❌ Error: reason is required (the admin will see it)"

    # Try the self-serve path first.
    status, msg = await _try_enable_directly(mcp_name)
    if status in ("enabled", "already_enabled"):
        return msg or "ok"
    if status == "fail":
        return msg or "❌ Error: unknown failure during direct-enable check"
    # status == "not_visible" → fall through to the admin request flow.

    try:
        result = await _post(
            f"/v1/agents/{AGENT_NAME}/mcp-requests",
            {"mcp_name": mcp_name, "reason": reason},
        )
    except _ApiError as exc:
        return f"❌ Error: {exc}"
    _cache_invalidate()
    rid = (result or {}).get("id")
    req_status = (result or {}).get("status") or "pending"
    return (
        f"✅ Request #{rid} submitted (mcp=`{mcp_name}`, status={req_status}). "
        f"The admin will be notified — this MCP isn't currently available "
        f"to **{AGENT_NAME}** (either not installed on the platform, or "
        f"requires admin instance configuration)."
    )


async def _handle_request_install(args: dict) -> str:
    return await _create_request(
        args.get("mcp_name", "").strip(),
        args.get("reason", "").strip(),
    )


async def _handle_request_access(args: dict) -> str:
    return await _create_request(
        args.get("mcp_name", "").strip(),
        args.get("reason", "").strip(),
    )


async def _handle_disable_mcp(args: dict) -> str:
    """PUT the agent's enabled list with the named MCP filtered out."""
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    mcp_name = args.get("mcp_name", "").strip()
    if not mcp_name:
        return "❌ Error: mcp_name is required"
    try:
        data = await _get(f"/v1/agents/{AGENT_NAME}/mcps")
    except _ApiError as exc:
        return f"❌ Error: {exc}"
    visible = (data or {}).get("mcps", [])
    entry = next((m for m in visible if m.get("name") == mcp_name), None)
    if entry is None:
        return (
            f"ℹ️  `{mcp_name}` isn't visible to **{AGENT_NAME}** — nothing to "
            f"disable. (Either it was never installed on the platform, or "
            f"the admin revoked authorization.)"
        )
    if not entry.get("enabled"):
        return (
            f"ℹ️  `{mcp_name}` is already disabled for **{AGENT_NAME}**."
        )
    remaining = [m.get("name") for m in visible if m.get("enabled") and m.get("name") != mcp_name]
    try:
        await _put(f"/v1/agents/{AGENT_NAME}/mcps", {"mcps": remaining})
    except _ApiError as exc:
        return f"❌ Error: {exc}"
    _cache_invalidate()
    return f"✅ `{mcp_name}` disabled for **{AGENT_NAME}**."


async def _handle_get_request_status(args: dict) -> str:
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    rid = args.get("request_id")
    if not isinstance(rid, int):
        return "❌ Error: request_id (integer) is required"
    try:
        data = await _get(f"/v1/agents/{AGENT_NAME}/mcp-requests")
    except _ApiError as exc:
        return f"❌ Error: {exc}"
    rows = (data or {}).get("requests", [])
    row = next((r for r in rows if int(r.get("id", 0)) == rid), None)
    if row is None:
        return f"❌ Error: request #{rid} not found for agent {AGENT_NAME}"
    parts = [
        f"Request #{rid}: **{row.get('status')}**",
        f"mcp=`{row.get('mcp_name')}` type={row.get('request_type')}",
    ]
    if note := row.get("admin_note"):
        parts.append(f"admin_note: {note}")
    if resolved_at := row.get("resolved_at"):
        resolver = row.get("resolved_by_name") or row.get("resolved_by_email") or "admin"
        parts.append(f"resolved at {resolved_at} by {resolver}")
    return "\n".join(parts)


async def _handle_cancel_request(args: dict) -> str:
    if not AGENT_NAME:
        return "❌ Error: agent context unavailable"
    rid = args.get("request_id")
    if not isinstance(rid, int):
        return "❌ Error: request_id (integer) is required"
    try:
        await _post(f"/v1/agents/{AGENT_NAME}/mcp-requests/{rid}/cancel")
    except _ApiError as exc:
        return f"❌ Error: {exc}"
    _cache_invalidate()
    return f"✅ Request #{rid} cancelled."


_DISPATCH = {
    "list_enabled_mcps": _handle_list_enabled_mcps,
    "list_available_mcps": _handle_list_available_mcps,
    "list_community_mcps": _handle_list_community_mcps,
    "request_mcp_install": _handle_request_install,
    "request_mcp_access": _handle_request_access,
    "disable_mcp_for_agent": _handle_disable_mcp,
    "get_request_status": _handle_get_request_status,
    "cancel_my_request": _handle_cancel_request,
}


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in ENABLED_TOOLS:
        return [TextContent(
            type="text",
            text=f"❌ Error: tool '{name}' is not available for this session.",
        )]
    handler = _DISPATCH.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"❌ Unknown tool: {name}")]
    try:
        text = await handler(arguments or {})
    except _ApiError as exc:
        text = f"❌ Error: {exc}"
    except Exception as exc:  # noqa: BLE001 — last-resort guard
        text = f"❌ Unexpected error: {type(exc).__name__}: {exc}"
    return [TextContent(type="text", text=text)]


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _main() -> None:
    from mcp.server.stdio import stdio_server
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
