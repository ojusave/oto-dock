"""`sessions/index.json` growth bound.

`core.session.session_state._sessions` is append-only tracking metadata persisted to
`index.json`. `prune_dead_sessions()` drops entries whose `last_active` is older
than the TTL so the file can't grow unbounded — but it must keep recent entries
and never guess about entries it can't date.
"""

from datetime import datetime, timedelta, timezone

from core.session import session_state


def test_prune_drops_old_keeps_recent_undated_and_unparseable():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    old = (now - timedelta(days=session_state._SESSION_INDEX_TTL_DAYS + 20)).isoformat()
    recent = (now - timedelta(days=10)).isoformat()

    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            "old": {"created": True, "message_count": 3, "last_active": old},
            "recent": {"created": True, "message_count": 1, "last_active": recent},
            # no last_active → indistinguishable from a brand-new registration → keep
            "undated": {"created": True, "message_count": 0},
            # unparseable timestamp → don't guess, keep
            "bad": {"created": True, "last_active": "not-a-date"},
        })
        removed = session_state.prune_dead_sessions(now=now)
        assert removed == 1
        assert set(session_state._sessions) == {"recent", "undated", "bad"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


def test_prune_is_noop_when_all_recent():
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            "a": {"last_active": now.isoformat()},
            "b": {"last_active": (now - timedelta(days=1)).isoformat()},
        })
        assert session_state.prune_dead_sessions(now=now) == 0
        assert set(session_state._sessions) == {"a", "b"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


# ---------------------------------------------------------------------------
# reap_task_sessions — defense-in-depth backstop for the is_task leak
# (entries are normally popped on run completion in scheduler._run_task)
# ---------------------------------------------------------------------------


def test_reap_task_sessions_drops_leaked_keeps_recent_and_non_task(monkeypatch):
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    ttl = session_state._TASK_SESSION_REAP_TTL_SECONDS
    old = (now - timedelta(seconds=ttl + 60)).isoformat()
    recent = (now - timedelta(seconds=60)).isoformat()
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            # leaked task session, older than TTL → reaped
            "old-task": {"is_task": True, "last_active": old},
            # task session still within TTL (maybe running) → kept
            "live-task": {"is_task": True, "last_active": recent},
            # legacy task stub with no timestamp → reaped (treated as stale)
            "stub-task": {"is_task": True, "created": True},
            # NON-task old session → untouched (prune_dead_sessions owns it)
            "old-chat": {"last_active": old},
        })
        removed = session_state.reap_task_sessions(now=now)
        assert removed == 2
        assert set(session_state._sessions) == {"live-task", "old-chat"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)


def test_reap_task_sessions_noop_when_recent_or_non_task(monkeypatch):
    now = datetime(2026, 6, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(session_state, "_save_sessions", lambda: None)

    saved = dict(session_state._sessions)
    try:
        session_state._sessions.clear()
        session_state._sessions.update({
            "live-task": {"is_task": True, "last_active": now.isoformat()},
            # old but NOT is_task → the task reaper must not touch it
            "chat": {"last_active": (now - timedelta(days=200)).isoformat()},
        })
        assert session_state.reap_task_sessions(now=now) == 0
        assert set(session_state._sessions) == {"live-task", "chat"}
    finally:
        session_state._sessions.clear()
        session_state._sessions.update(saved)
