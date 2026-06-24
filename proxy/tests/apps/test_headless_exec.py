"""Headless mcp_tool executor: real stdio-MCP end-to-end (spawn → call →
result), synthetic-session registration + full teardown, pool keying by
scope identity (the credential boundary), the personal-owner fail-closed
guard, self-heal on unknown tools, and the idle reaper.

``_build_session_parts`` is stubbed (the blocking identity/sandbox build
composes ``resolve_task_identity``/``resolve_visibility``/
``resolve_sandbox_config``, each covered by its own suite); everything from
the manager up — including the REAL fake MCP subprocess over stdio — runs
live here.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

import config
from auth.path_policy import SecurityContext
from core.session.session_state import get_session_security
from services.apps import headless_exec as hx
from storage import database as task_store

AGENT = "hx-agent"

FAKE_SERVER = """\
import json, os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def echo(text: str = "", flag: bool = False) -> str:
    return json.dumps({"text": text, "flag": flag,
                       "sid": os.environ.get("OTO_SESSION_ID", "")})


mcp.run()
"""


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    hx._pool.clear()
    hx._key_locks.clear()
    hx._inflight.clear()
    hx._selfheal_at.clear()
    hx._start_sem = asyncio.Semaphore(hx.START_CONCURRENCY)
    # No background sweeper in tests — reap is driven via _sweep_once.
    monkeypatch.setattr(hx, "_ensure_sweeper", lambda: None)
    yield
    # Every test must dispose what it started (close_all inside its loop) —
    # a leaked entry means leaked MCP subprocesses.
    assert not hx._pool


@pytest.fixture
def fake_mcp(tmp_path, monkeypatch):
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER)
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"test-mcp": {
        "type": "stdio", "command": sys.executable, "args": [str(server)],
    }}}))

    class _NoWrap:
        """Stand-in SandboxBuilder (bwrap needs the full platform mount tree)."""
        class cfg:
            username = "alice"
            role = "manager"

        def build_command_prefix(self, cmd):
            return list(cmd)

        def get_env_overrides(self):
            return {}

    def fake_build(agent, row):
        ctx = SecurityContext(role="manager", username=row.get("username") or "",
                              agent=agent, is_admin_agent=False)
        return cfg, {}, {}, _NoWrap(), ctx

    monkeypatch.setattr(hx, "_build_session_parts", fake_build)
    return cfg


def _shared_row() -> dict:
    return {"id": "hx-app-1", "agent": AGENT, "slug": "board",
            "username": "", "owner_sub": None}


def _personal_row(username: str = "hx-alice", sub: str = "hx-alice-sub") -> dict:
    return {"id": "hx-app-2", "agent": AGENT, "slug": "mine",
            "username": username, "owner_sub": sub}


ACTION = {"id": "run", "label": "Run", "type": "mcp_tool",
          "mcp": "test-mcp", "tool": "echo"}


def test_execute_end_to_end_and_reuse(fake_mcp):
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION,
                                      {"text": "hi", "flag": True})
        assert r["status"] == "done"
        payload = json.loads(r["result"])
        assert payload["text"] == "hi" and payload["flag"] is True
        # The subprocess ran under the SYNTHETIC session id…
        assert payload["sid"].startswith("appx-")
        # …whose security context is registered for hook callbacks.
        entry = hx._pool[(AGENT, "")]
        assert entry.session_id == payload["sid"]
        assert get_session_security(entry.session_id) is not None

        # Warm reuse: second click, same manager (no respawn).
        first_manager = entry.manager
        r2 = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"})
        assert json.loads(r2["result"])["text"] == "again"
        assert hx._pool[(AGENT, "")].manager is first_manager

        sid = entry.session_id
        await hx.close_all()
        assert hx._pool == {}
        # Full synthetic-session teardown — context gone, JWT replay denied.
        assert get_session_security(sid) is None

    asyncio.run(main())


def test_pool_keyed_by_scope_identity(fake_mcp):
    task_store.upsert_user("hx-alice-sub", "hx-alice@test.com", "Alice", "member")
    task_store.add_user_agent("hx-alice-sub", AGENT, "manager", "test")

    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {})
        await hx.execute_app_tool(_personal_row(), ACTION, {})
        # Personal and shared managers never share a key (credential boundary).
        assert set(hx._pool) == {(AGENT, ""), (AGENT, "hx-alice-sub")}
        assert (hx._pool[(AGENT, "")].manager
                is not hx._pool[(AGENT, "hx-alice-sub")].manager)
        await hx.close_all()

    asyncio.run(main())


def test_personal_owner_must_still_hold_access(fake_mcp):
    task_store.upsert_user("hx-bob-sub", "hx-bob@test.com", "Bob", "member")
    # Bob holds NO role on the agent (unassigned since pin/approval).
    row = _personal_row(username="hx-bob", sub="hx-bob-sub")

    async def main():
        r = await hx.execute_app_tool(row, ACTION, {})
        assert r["status"] == "error"
        assert "no longer has access" in r["reason"]
        assert hx._pool == {}  # denied BEFORE any manager was built

    asyncio.run(main())


def test_unknown_tool_on_fresh_build_never_rebuilds(fake_mcp):
    """A manager built by THIS call that lacks the tool means the MCP just
    failed to start — rebuilding immediately would double the cold-start
    cost for nothing."""
    async def main():
        assert hx._pool == {}
        bad = dict(ACTION, tool="nope")
        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error" and "not available" in r["reason"]
        assert hx._selfheal_at == {}  # no self-heal was attempted
        await hx.close_all()

    asyncio.run(main())


def test_dead_server_self_heals_on_next_click(fake_mcp):
    """A server that died AFTER tool discovery (transport failure — the
    uptime-kuma case: startup auth failed against a blocked port, warm
    manager kept erroring forever) reports its tools missing via has_tool,
    so the next click rebuilds and succeeds."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {})
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "")].manager
        first.servers["test-mcp"].dead = True  # what a failed call_tool sets

        r = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "again"})
        assert r["status"] == "done"
        assert json.loads(r["result"])["text"] == "again"
        assert hx._pool[(AGENT, "")].manager is not first  # rebuilt
        await hx.close_all()

    asyncio.run(main())


def test_self_heal_rebuilds_warm_manager_once_per_cooldown(fake_mcp):
    """A WARM manager missing the tool is dropped and rebuilt (the MCP may
    have been re-enabled since it was built) — but at most once per
    cooldown, so a permanently-flaky MCP can't turn every click into a
    full manager rebuild (found live on the trusted VM: uptime-kuma)."""
    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION, {})
        assert r["status"] == "done"
        first = hx._pool[(AGENT, "")].manager

        bad = dict(ACTION, tool="nope")
        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error" and "not available" in r["reason"]
        second = hx._pool[(AGENT, "")].manager
        assert second is not first  # self-heal rebuilt the manager

        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error"
        assert hx._pool[(AGENT, "")].manager is second  # cooldown: no rebuild

        # Cooldown elapsed → the self-heal path opens again.
        hx._selfheal_at[(AGENT, "")] -= hx._SELF_HEAL_COOLDOWN_S + 1
        r = await hx.execute_app_tool(_shared_row(), bad, {})
        assert r["status"] == "error"
        assert hx._pool[(AGENT, "")].manager is not second
        await hx.close_all()

    asyncio.run(main())


def test_inflight_duplicate_rejected_but_distinct_args_pass(fake_mcp):
    """The flight key is args-aware: an IDENTICAL repeat is rejected while
    the first call runs, but the same declared action with different args
    (one parameterized action serving many widgets) is an independent call."""
    import hashlib as _hl
    import json as _json

    def _fp(args: dict) -> str:
        return _hl.sha256(_json.dumps(
            args, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:16]

    async def main():
        hx._inflight.add(("hx-app-1", f"run|{_fp({})}"))
        try:
            r = await hx.execute_app_tool(_shared_row(), ACTION, {})
            assert r["status"] == "error" and "already running" in r["reason"]
            # Same action id, different args → runs (real subprocess call).
            r2 = await hx.execute_app_tool(_shared_row(), ACTION, {"text": "b"})
            assert r2["status"] == "done"
        finally:
            hx._inflight.clear()
        await hx.close_all()

    asyncio.run(main())


def test_result_truncation(fake_mcp, monkeypatch):
    monkeypatch.setattr(hx, "RESULT_MAX_CHARS", 8)

    async def main():
        r = await hx.execute_app_tool(_shared_row(), ACTION,
                                      {"text": "0123456789abcdef"})
        assert r["status"] == "done"
        assert r["result"].endswith("(result truncated)")
        await hx.close_all()

    asyncio.run(main())


def test_idle_reap_full_teardown(fake_mcp, monkeypatch):
    async def main():
        await hx.execute_app_tool(_shared_row(), ACTION, {})
        entry = hx._pool[(AGENT, "")]
        sid = entry.session_id
        monkeypatch.setattr(config, "get_idle_timeout", lambda: 1)
        entry.last_used -= 10
        entry.manager.last_activity -= 10
        await hx._sweep_once()
        assert hx._pool == {}
        assert get_session_security(sid) is None

    asyncio.run(main())
