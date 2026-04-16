#!/usr/bin/env python3
"""PreToolUse hook: calls the proxy to decide whether to allow or deny a tool use.

The proxy decides based on session mode and client type:
  - "auto" mode: always allow (phone, tasks)
  - "default" mode + dashboard: block until user approves/denies in dashboard UI
  - "plan" mode: always deny (read-only planning)
  - AskUserQuestion: always deny (question shown to user, they reply in next message)

Environment variables (set by core/sandbox/env_builder.py in the CLI's subprocess env):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID - session UUID for this conversation
"""

import json
import os
import sys
import time
import urllib.request

# Transport failures fail CLOSED. The alternative — silently allowing the
# tool a human was being asked to approve — turns every proxy restart or
# tunnel drop into an auto-approval. The short retry ladder rides out a
# proxy reboot; the satellite loopback tunnel answers fast with synthetic
# 502/503 once its WS is down, so a dead platform never hangs the CLI here.
_RETRY_DELAYS = (2.0, 4.0)


def _request_decision(req):
    """POST to the proxy, retrying transient failures. None = unreachable."""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=604800) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    return None


def main():
    # Read hook input from stdin
    try:
        inp = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        inp = {}

    session_id = os.environ.get("OTO_SESSION_ID", "")
    proxy_url = os.environ.get("PROXY_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")

    # If env vars aren't set (e.g. subagent call), allow by default
    if not proxy_url or not api_key:
        return

    tool_name = inp.get("tool_name", "")
    tool_input = inp.get("tool_input", {})

    payload = json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/permission",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    result = _request_decision(req)
    if result is None:
        decision = "deny"
        reason = "OtoDock platform unreachable — tool call denied (fail closed)"
        updated_input = None
    else:
        decision = result.get("decision", "allow")
        reason = result.get("reason", "")
        updated_input = result.get("updated_input")

    # "defer" (interactive TUI ask-tier): emit NO decision so Claude's own
    # permission system — its native in-terminal prompt + Shift+Tab modes —
    # decides. Exit 0 with no output = "the hook has no opinion" (≠ "ask", which
    # would force a prompt and defeat Shift+Tab). Hard denies still come through
    # as "deny" above.
    if decision == "defer":
        return

    # Codex's PreToolUse hook supports a DENY decision but REJECTS
    # permissionDecision:"allow" ("unsupported permissionDecision:allow"). Under
    # Codex (OTO_HOOK_DENY_ONLY=1, set on the interactive spawn) emit JSON only to
    # DENY; any non-deny becomes "no opinion" (exit 0) → the CLI proceeds and
    # Codex's own sandbox / -a on-request handles any prompt. Claude supports
    # "allow" and needs it (esp. headless), so this gate is Codex-only.
    if decision != "deny" and os.environ.get("OTO_HOOK_DENY_ONLY"):
        return

    # Output in Claude Code's expected hookSpecificOutput format
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if decision == "deny" and not reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = "Denied by user"
    # Remote satellites: the proxy rewrote a sandbox-virtual / `~` path arg to
    # its satellite-host form — hand the CLI the corrected input so the tool
    # runs against the real path (allow-only; deny/defer never rewrite).
    if decision == "allow" and isinstance(updated_input, dict):
        output["hookSpecificOutput"]["updatedInput"] = updated_input

    print(json.dumps(output))


if __name__ == "__main__":
    main()
