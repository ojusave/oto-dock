"""Execution-mode resolver + the global interactive kill-switch.

Decides whether a session spawns as the native interactive TUI or the headless
``-p`` stream. This resolver is the SINGLE conceptual point that makes
server-driven interactive (and thus the billing mitigation) removable in one
flag, and makes the meetings override deterministic.

Precedence (highest first):
  1. **Meetings override** — meeting participants ALWAYS run headless ``-p`` (a
     single PTY can't host a multi-agent meeting). Overrides everything.
  2. **Global kill-switch** — if interactive is disabled platform-wide, every
     session falls back to ``-p``. No rip-out: the task/chat systems are
     unchanged, only the requested spawn mode flips.
  3. **Per-chat toggle** — a live user choice on one chat.
  4. **Per-agent default execution mode** — a manager-level agent setting.
  5. **Platform default** — ``-p``.

NOTE: there is no single call site that resolves the execution *layer*
(``get_execution_layer`` is called at 6+ sites), so this resolver is consulted
at each spawn entry point, and its result rides on ``AgentConfig.interactive``.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("claude-proxy.execution_mode")

INTERACTIVE = "interactive"
HEADLESS = "-p"
_VALID = (INTERACTIVE, HEADLESS)

# platform_settings key for the global kill-switch.
KILL_SWITCH_KEY = "interactive_cli_enabled"


def parse_enabled(val: object) -> bool:
    """Truthy parse for the kill-switch setting value (shared with the
    admin settings API so the two can't drift)."""
    return str(val or "").strip().lower() in ("1", "true", "yes", "on")


def is_interactive_enabled() -> bool:
    """The global kill-switch. Defaults to OFF (``-p``) unless an admin enables
    it — interactive is opt-in and removable in one flag."""
    from storage import database
    try:
        val = database.get_platform_setting(KILL_SWITCH_KEY)
    except Exception:
        return False
    return parse_enabled(val)


def resolve_execution_mode(
    *,
    agent_default: str = "",
    chat_override: str | None = None,
    is_meeting: bool = False,
) -> str:
    """Return ``INTERACTIVE`` or ``HEADLESS`` for a session about to spawn.

    ``agent_default`` / ``chat_override`` accept ``"interactive"`` / ``"-p"``
    (anything else is treated as unset and falls through).
    """
    # 1. Meetings always run headless — overrides everything below.
    if is_meeting:
        return HEADLESS
    # 2. Global kill-switch.
    if not is_interactive_enabled():
        return HEADLESS
    # 3. Per-chat toggle.
    if chat_override in _VALID:
        return chat_override
    # 4. Per-agent default execution mode.
    if agent_default in _VALID:
        return agent_default
    # 5. Platform default.
    return HEADLESS


def is_interactive(
    *,
    agent_default: str = "",
    chat_override: str | None = None,
    is_meeting: bool = False,
) -> bool:
    """Convenience wrapper → ``True`` when the resolved mode is interactive."""
    return resolve_execution_mode(
        agent_default=agent_default,
        chat_override=chat_override,
        is_meeting=is_meeting,
    ) == INTERACTIVE
