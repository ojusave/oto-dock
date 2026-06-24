"""The task permission mode ``auto`` is treated like ``dontAsk`` in the
dashboard permission hook, so a continued (re-warmed) task does not prompt on
MCP/bash tools. Regression guard: switching the continued task to ``default``
must STILL prompt.

The dashboard early-return at hooks.py:627 (`mode in ("dontAsk", "auto")`) is
the functional gate for a continued task — it short-circuits before the bash
tier logic, so an ``auto`` session auto-approves every tool.
"""

import asyncio
import os
import sys

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from api.hooks.hooks import hook_permission, HookPermissionRequest  # noqa: E402
from core.session import session_state # noqa: E402


@pytest.fixture
def dashboard_session(monkeypatch):
    """A dashboard session with a real (local, admin) security context — so the
    Pass 1 path check ALLOWS and the hook reaches the Pass 2 mode logic — plus a
    no-op session-match check. Post-B4 every live session carries a context (a
    None now fail-closes at Pass 1), so this mirrors production instead of relying
    on the old fail-open skip."""
    from auth.path_policy import SecurityContext
    sid = "sess-auto-test"
    monkeypatch.setattr("api.hooks.hooks.verify_session_match", lambda *a, **k: None)
    session_state._sessions[sid] = {"client_type": "dashboard"}
    session_state._session_security[sid] = SecurityContext(
        role="admin", username="", agent="demo", is_admin_agent=True,
    )
    yield sid
    session_state._sessions.pop(sid, None)
    session_state._session_modes.pop(sid, None)
    session_state._session_security.pop(sid, None)
    session_state._session_tool_allows.pop(sid, None)
    # Drop the prompt queue too — a timed-out prompt left queued by one test
    # must not be read as the NEXT test's prompt.
    session_state._permission_emitters.pop(sid, None)


async def _decide(sid, tool="mcp__demo__do_thing", tool_input=None):
    req = HookPermissionRequest(session_id=sid, tool_name=tool, tool_input=tool_input or {})
    return await hook_permission(req, authorization=None)


@pytest.mark.asyncio
async def test_auto_allows_mcp_tool(dashboard_session):
    """A continued task (mode=auto) auto-approves an MCP tool — no prompt."""
    session_state.set_session_mode(dashboard_session, "auto")
    assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_auto_allows_bash(dashboard_session):
    """mode=auto short-circuits at 627 → bash is allowed without tier prompting."""
    session_state.set_session_mode(dashboard_session, "auto")
    decision = await _decide(dashboard_session, tool="Bash", tool_input={"command": "echo hi"})
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_dontask_allows_mcp_tool(dashboard_session):
    """Baseline (unchanged): dontAsk auto-approves."""
    session_state.set_session_mode(dashboard_session, "dontAsk")
    assert (await _decide(dashboard_session))["decision"] == "allow"


@pytest.mark.asyncio
async def test_default_still_prompts(dashboard_session):
    """Regression: switching a continued task to 'default' must PROMPT (block on
    the permission queue), not auto-approve. A timeout proves it's waiting."""
    session_state.set_session_mode(dashboard_session, "default")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(dashboard_session), timeout=0.4)


async def _approve_next_prompt(sid) -> dict:
    """Pop the next queued permission prompt for ``sid`` and approve it."""
    queue = session_state.get_permission_queue(sid)
    prompt = await asyncio.wait_for(queue.get(), timeout=2)
    assert prompt["event_type"] == "permission_prompt"
    session_state.resolve_permission(prompt["request_id"], True)
    return prompt


@pytest.mark.asyncio
async def test_default_remembers_mcp_allow_for_session(dashboard_session):
    """One Allow per MCP tool per session: after the user approves an MCP tool
    in default mode, later calls to the SAME tool auto-approve instead of
    raising a fresh card per call (7 ha_search calls = 7 cards was the bug)."""
    session_state.set_session_mode(dashboard_session, "default")
    task = asyncio.create_task(_decide(dashboard_session))
    await _approve_next_prompt(dashboard_session)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "allow"
    # Second call to the same tool: allowed WITHOUT blocking on a prompt.
    decision = await asyncio.wait_for(_decide(dashboard_session), timeout=2)
    assert decision["decision"] == "allow"


@pytest.mark.asyncio
async def test_default_mcp_deny_not_remembered(dashboard_session):
    """A Deny is never remembered — the next call prompts again."""
    session_state.set_session_mode(dashboard_session, "default")
    task = asyncio.create_task(_decide(dashboard_session))
    queue = session_state.get_permission_queue(dashboard_session)
    prompt = await asyncio.wait_for(queue.get(), timeout=2)
    session_state.resolve_permission(prompt["request_id"], False)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "deny"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(dashboard_session), timeout=0.4)


@pytest.mark.asyncio
async def test_default_allow_scoped_to_exact_tool(dashboard_session):
    """The remembered allow is keyed by the full tool name — a different tool
    on the same MCP server still prompts."""
    session_state.set_session_mode(dashboard_session, "default")
    task = asyncio.create_task(_decide(dashboard_session))
    await _approve_next_prompt(dashboard_session)
    await asyncio.wait_for(task, timeout=2)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _decide(dashboard_session, tool="mcp__demo__other_thing"), timeout=0.4)


@pytest.mark.asyncio
async def test_default_bash_allow_not_remembered(dashboard_session):
    """Non-MCP tools never enter the allow-memory: Bash risk varies per
    command, so an approved ask-tier command doesn't blanket-allow the next."""
    session_state.set_session_mode(dashboard_session, "default")
    task = asyncio.create_task(_decide(
        dashboard_session, tool="Bash", tool_input={"command": "frobnicate --hard"}))
    await _approve_next_prompt(dashboard_session)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "allow"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(
            dashboard_session, tool="Bash",
            tool_input={"command": "frobnicate --hard"}), timeout=0.4)


@pytest.mark.asyncio
async def test_high_risk_device_tool_allow_not_remembered(dashboard_session, monkeypatch):
    """High-risk device tools (raw-RCE grade) re-prompt per call BY DESIGN even
    after an Allow — they must never enter the session allow-memory."""
    from services.mcp import mcp_registry
    monkeypatch.setattr(mcp_registry, "is_high_risk_device_tool", lambda s, t: True)
    session_state.set_session_mode(dashboard_session, "default")
    task = asyncio.create_task(_decide(dashboard_session))
    await _approve_next_prompt(dashboard_session)
    assert (await asyncio.wait_for(task, timeout=2))["decision"] == "allow"
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_decide(dashboard_session), timeout=0.4)


@pytest.mark.asyncio
async def test_session_allow_memory_cleared_on_session_close(dashboard_session):
    """The allow set dies with the session's permission state."""
    session_state.remember_session_tool_allow(dashboard_session, "mcp__demo__do_thing")
    assert session_state.is_session_tool_allowed(dashboard_session, "mcp__demo__do_thing")
    session_state.cleanup_session_permission_state(dashboard_session)
    assert not session_state.is_session_tool_allowed(dashboard_session, "mcp__demo__do_thing")
