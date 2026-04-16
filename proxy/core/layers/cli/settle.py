"""Claude Code CLI settle orchestration — pure decision logic.

Encapsulates the post-`result` read-loop decisions that decide when a turn
is truly over: how long to wait between stdout lines, whether to extend the
wait on hook activity, when to exit settle mode.

Pure: no I/O, no subprocess state. Inputs are the translator's cross-turn
counters, the clock, and `get_hook_activity(session_id)`. Shared between
local (`PersistentSession.send_message`) and remote (`RemoteExecutionLayer
.send_message`) so both paths produce identical turn-end semantics.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from core.layers.cli.translator import ClaudeCLIEventTranslator

logger = logging.getLogger("cli-settle")

# Max time settle will hold open while subagents are still pending. The
# SubagentStop hook is the deterministic completion signal; this ceiling is
# only the backstop for a genuinely lost hook (so a dropped Stop can't hang a
# task forever holding the session lock). Generous, since real background work
# (e.g. a research subagent) legitimately runs for minutes.
_SETTLE_PENDING_CEILING = 600.0

# --- Foreign-result gating (interactive turns) -----------------------------
#
# After a ``claude --resume`` respawn, a `result` event can reach the turn
# loop that does NOT belong to the driven prompt: the resume handshake
# mini-turn ("Continue from where you left off." → "No response requested."
# → result — its messages land only in the session JSONL, so zero content
# streams before its result), or a stale result from the replaced process's
# dying flush racing past the send-start drain. Ending the turn there
# orphans the real answer (the Mode D incident: pump ended blocks=0 one
# second after send; the answer was written to the JSONL 50s later with no
# pump attached). These helpers let both turn loops re-arm across such
# results instead of closing the turn.

# The CLI's fixed handshake reply. String-matching CLI-synthesized text is
# tolerable because the fleet CLI version is pinned (VERSIONS.md); the
# content-count guard below catches the shape even if this string drifts.
RESUME_HANDSHAKE_RESULT = "No response requested."

# Pathology bound: never re-arm more than this many times per turn.
FOREIGN_RESULT_SKIP_CAP = 5

# After skipping a foreign result, how long to wait for the driven turn to
# produce anything before closing the turn anyway (a legit empty-content
# result that we mis-skipped must not hang the turn forever).
FOREIGN_SKIP_SILENCE_S = 60.0


def is_foreign_result(raw: dict, content_chunks: int) -> bool:
    """True when a `result` event cannot plausibly close the driven turn.

    Error results always close the turn (abort/failure paths depend on it).
    A success result with ZERO content streamed under this turn is a
    replayed/stale result; one whose text is the resume-handshake sentinel
    with at most a stray chunk or two is the handshake mini-turn."""
    if raw.get("is_error") or raw.get("subtype") != "success":
        return False
    if content_chunks == 0:
        return True
    return (raw.get("result", "") == RESUME_HANDSHAKE_RESULT
            and content_chunks <= 2)


def chunk_is_content(chunk) -> bool:
    """Does this translator chunk prove the driven turn is streaming?

    thinking `progress` pings are token-count gauges (emitted for adaptive-
    effort models), not content — they must not defeat the zero-content
    foreign-result check."""
    et = chunk.event_type
    if et == "text":
        return bool(chunk.text.strip())
    if et == "thinking":
        return chunk.event_data.get("phase") != "progress"
    return et in ("tool_start", "tool_info", "task_spawn", "delegate_spawn")


class SettleController:
    """Decides when a CLI turn's stdout read loop should end.

    Lifecycle:
        1. Caller constructs with `settle_after_result` and translator.
        2. Caller reads stdout events, feeds translator.
        3. On `result` event, caller calls `enter_settle()`.
           (If `settle_after_result == 0`, `is_interactive_done()` is True
           and the caller exits immediately.)
        4. In settle mode, caller uses `effective_timeout()` as the
           readline timeout. On timeout, caller calls
           `should_exit_on_silence(silence_duration)` to decide whether to
           exit or keep waiting (hook activity may still be running).
    """

    __slots__ = (
        "session_id",
        "settle_after_result",
        "translator",
        "_get_hook_activity",
        "_settling",
        "_settle_start",
        "_last_heartbeat",
    )

    def __init__(
        self,
        session_id: str,
        settle_after_result: float,
        translator: ClaudeCLIEventTranslator,
        *,
        get_hook_activity: Callable[[str], float | None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.settle_after_result = settle_after_result
        self.translator = translator
        if get_hook_activity is None:
            from core.session.session_state import get_hook_activity as _gha
            get_hook_activity = _gha
        self._get_hook_activity = get_hook_activity

        self._settling: bool = False
        self._settle_start: float = 0.0
        self._last_heartbeat: float = 0.0

    # --- Public API ---

    @property
    def settling(self) -> bool:
        """True once enter_settle() has been called."""
        return self._settling

    def is_interactive_done(self) -> bool:
        """True if no settle was requested (interactive chat path)."""
        return self.settle_after_result <= 0

    def _registry(self):
        from core.session.session_state import get_subagent_registry
        return get_subagent_registry(self.session_id)

    def _bg_registry(self):
        from core.events.bg_command_state import get_bg_command_registry
        return get_bg_command_registry(self.session_id)

    def enter_settle(self) -> None:
        """Called by the I/O loop right after the `result` event.

        Resets the translator's per-turn parsing state and starts the settle
        timer. Subagent pending state lives in the SubagentRegistry.
        """
        self._settling = True
        self._settle_start = time.monotonic()
        self._last_heartbeat = self._settle_start
        self.translator.reset_for_settle()
        logger.info(
            f"[{self.session_id[:8]}] entering settle "
            f"(agents_spawned={self.translator.agents_spawned}, "
            f"pending={self._registry().pending_count}, "
            f"base_timeout={self.settle_after_result}s)"
        )

    def effective_timeout(self) -> float:
        """The readline timeout to use on the next iteration.

        Pre-settle: a generous 60s heartbeat window. In settle we use a short
        5s slice so the loop re-checks the SubagentRegistry promptly — whether
        there's nothing pending (exit on the next silence) or background agents
        are still finishing (keep waiting while their hooks fire). This also
        fast-settles a no-subagent task instead of idling the full base
        timeout.
        """
        if not self._settling:
            return 60.0  # pre-settle heartbeat timeout
        return 5.0

    def should_exit_on_silence(self, silence_duration: float) -> bool:
        """Called after stdout has been silent for `silence_duration` seconds.

        Returns True if the read loop should exit (turn is done).
        Returns False if the loop should keep waiting — background subagents are
        still working (their SubagentStop hooks haven't all landed) OR background
        bash commands are still running (their task_updated{completed} hasn't
        arrived on stdout yet).
        """
        if not self._settling:
            return False

        reg = self._registry()
        bgreg = self._bg_registry()
        if not reg.has_pending and not bgreg.has_pending:
            # Every tracked subagent AND background command finished (or none
            # spawned) → turn over.
            return True

        # Pending subagents: TRUST the SubagentStop hook — do NOT settle on
        # stdout/hook silence. A subagent that's sleeping or doing slow
        # non-tool work fires no hooks, so "silence" does NOT mean "done";
        # exiting here would return control before it finishes and (critically)
        # let a delegate task report back missing its subagents' results.
        # Pending bg commands: their completion (task_updated{completed}) lands
        # on THIS stdout stream and the 5s settle slices keep reading it — so
        # staying in settle is exactly what lets the task observe completion
        # before it returns + synthesizes. The registry flips the moment the
        # hook lands (subagents) / the frame is parsed (commands). The ceiling
        # is the sole backstop for a genuinely lost signal.
        if (time.monotonic() - self._settle_start) > _SETTLE_PENDING_CEILING:
            logger.warning(
                f"[{self.session_id[:8]}] settle: {reg.pending_count} subagent(s) + "
                f"{bgreg.pending_count} bg-command(s) still pending after "
                f"{_SETTLE_PENDING_CEILING:.0f}s ceiling — settling"
            )
            return True
        return False

    def maybe_log_heartbeat(self, *, proc_alive: bool = True) -> None:
        """Emit a periodic progress heartbeat during long settles.

        Called once per loop iteration. Only logs every 30s.
        Mirrors PersistentSession.send_message lines 561-573.
        """
        if not self._settling:
            return
        now = time.monotonic()
        if now - self._last_heartbeat < 30.0:
            return
        self._last_heartbeat = now
        elapsed = now - self._settle_start
        last_hook = self._get_hook_activity(self.session_id)
        hook_ago = f"{now - last_hook:.1f}s ago" if last_hook else "never"
        reg = self._registry()
        logger.info(
            f"[{self.session_id[:8]}] settle heartbeat — "
            f"elapsed={elapsed:.0f}s, "
            f"agents_spawned={self.translator.agents_spawned}, "
            f"pending={reg.pending_count}, last_hook={hook_ago}, "
            f"process_alive={proc_alive}"
        )

    def log_presettle_heartbeat(
        self, *, agents_spawned: int, proc_alive: bool,
    ) -> None:
        """Log a pre-settle heartbeat after 60s of no stdout activity.

        Mirrors PersistentSession.send_message lines 602-617.
        """
        last_hook = self._get_hook_activity(self.session_id)
        hook_ago = (
            f"{time.monotonic() - last_hook:.1f}s ago" if last_hook else "never"
        )
        logger.info(
            f"[{self.session_id[:8]}] pre-settle heartbeat — "
            f"no stdout for 60s, agents_spawned={agents_spawned}, "
            f"last_hook={hook_ago}, process_alive={proc_alive}"
        )
