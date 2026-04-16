"""Live status of a delegation lane's chat — derived fresh, nothing persisted.

``generating`` — a turn is producing output right now (headless pump or open
interactive turn). ``awaiting_user`` — the lane is blocked on a human (a
permission dialog on either lane kind). ``idle`` — nothing live.

Reads the same in-memory sources the dashboard reconnect path uses:
``_chat_streaming_state`` / ``_active_pumps`` for pump lanes and the live
interactive-session registry for PTY lanes. Cheap and synchronous — safe to
call per row when listing sessions.
"""

from core.events.stream_pump import _active_pumps
from core.session import interactive_session
from core.session.session_state import (
    _chat_streaming_state,
    _session_permission_requests,
)

STATUS_GENERATING = "generating"
STATUS_AWAITING_USER = "awaiting_user"
STATUS_IDLE = "idle"


def chat_status(chat_id: str) -> str:
    """The lane's live status: generating | awaiting_user | idle."""
    if not chat_id:
        return STATUS_IDLE

    st = _chat_streaming_state.get(chat_id)
    if st:
        # A pending permission outranks streaming — the turn is stalled on the
        # human either way.
        if st.get("pending_permission"):
            return STATUS_AWAITING_USER
        if st.get("streaming") or st.get("live_blocks"):
            return STATUS_GENERATING

    pump = _active_pumps.get(chat_id)
    if pump is not None and not pump.is_done:
        return STATUS_GENERATING

    live = interactive_session.find_live_for_chat(chat_id)
    if live is not None:
        # The gates report "turn_open" before "permission_pending", and a hook
        # dialog usually appears MID-turn — probe the permission table directly.
        if _session_permission_requests.get(live.session_id):
            return STATUS_AWAITING_USER
        # A parked question (the AskUserQuestion / request_user_input fold,
        # 2026-07-10) CLOSES the turn while the chat waits on the human — the
        # chat row shows "needs your input"; the lane must match, not idle.
        if getattr(live, "question_parked", False):
            return STATUS_AWAITING_USER
        if live._turn_open:
            return STATUS_GENERATING

    return STATUS_IDLE
