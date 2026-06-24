"""Interactive delegated-worker parity — the run-lifecycle half of delegation.

A delegated task that RUNS interactive must complete exactly like a headless
one: `_run_interactive_task` awaits the tailer's turn-end signal (registered
BEFORE the cold prompt is injected so no turn can complete unobserved), the
tailer has already persisted the turns to chat_messages, and the shared
`_collect_task_output` + `_deliver_task_result` produce an identical payload.

Run individually (conftest DB-pool gotcha):
    venv/bin/python -m pytest tests/tasks/test_interactive_worker_parity.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from core.session import interactive_session
from services.scheduler import scheduler
from storage import database as task_store


class _FakeInteractiveSession:
    """The slice `_run_interactive_task` drives: the completion callback slot,
    the cold-prompt injection, and liveness."""

    def __init__(self):
        self.on_turn_complete = None
        self.alive = True
        self.had_viewer = False              # real sessions track viewer attach
        self.events: list[str] = []          # call-order recorder
        self.submitted: list[str] = []

    def submit_prompt(self, text: str) -> None:
        self.events.append("submit")
        self.submitted.append(text)

    def set_callback_marker(self):
        # queried via property below; kept simple
        pass


@pytest.fixture()
def fake_isess(monkeypatch):
    fake = _FakeInteractiveSession()
    monkeypatch.setitem(interactive_session._sessions, "sid-worker", fake)

    # Record the registration order: setting on_turn_complete must precede the
    # prompt injection (a turn that completes instantly must not be missed).
    orig_setattr = _FakeInteractiveSession.__setattr__

    def _tracking_setattr(self, name, value):
        if name == "on_turn_complete" and value is not None and hasattr(self, "events"):
            self.events.append("callback_registered")
        orig_setattr(self, name, value)

    monkeypatch.setattr(_FakeInteractiveSession, "__setattr__", _tracking_setattr)
    yield fake
    interactive_session._sessions.pop("sid-worker", None)


def test_turn_end_completes_the_run(fake_isess):
    async def _run():
        task = asyncio.create_task(scheduler._run_interactive_task(
            "sid-worker", "chat-w", "analyze the repo", False,
        ))
        await asyncio.sleep(0.05)
        # Cold prompt injected via the PTY flush, AFTER the callback was armed.
        assert fake_isess.events == ["callback_registered", "submit"]
        assert fake_isess.submitted == ["analyze the repo"]
        assert not task.done()               # awaiting the turn-end signal
        # The tailer reports turn-end (bg-empty + min-turn gates live in
        # interactive_session; the scheduler just gets the callback).
        fake_isess.on_turn_complete("final answer text")
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())


def test_argv_prompt_is_not_reinjected(fake_isess):
    # Codex fresh delivers the cold prompt via the launch argv — injecting it
    # again would run the prompt twice.
    async def _run():
        task = asyncio.create_task(scheduler._run_interactive_task(
            "sid-worker", "chat-w", "argv prompt", True,
        ))
        await asyncio.sleep(0.05)
        assert fake_isess.submitted == []
        fake_isess.on_turn_complete("done")
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())


def test_unregistered_session_raises(temp_db):
    async def _run():
        with pytest.raises(RuntimeError, match="not registered"):
            await scheduler._run_interactive_task("sid-ghost", "chat-g", "p", False)

    asyncio.run(_run())


def test_dead_pty_fails_fast(fake_isess):
    # CLI crash / idle reap mid-run: the watcher must fail within one liveness
    # poll (~5s), not hang until the 2h max-time backstop.
    async def _run():
        fake_isess.alive = False
        with pytest.raises(RuntimeError, match="ended before completing"):
            await asyncio.wait_for(scheduler._run_interactive_task(
                "sid-worker", "chat-w", "p", False,
            ), timeout=15)

    asyncio.run(_run())


def test_delivery_payload_matches_headless(temp_db):
    """The parity core: output collection is the SAME function over the same
    chat_messages rows, whether the pump (headless) or the tailer (interactive)
    persisted them — so `_deliver_task_result` receives an identical payload."""
    task_store.create_chat("chat-headless", "u", "pa")
    task_store.create_chat("chat-interactive", "u", "pa")
    # Headless: the pump persists assistant messages turn by turn.
    task_store.add_chat_message("chat-headless", "user", "do the thing")
    task_store.add_chat_message("chat-headless", "assistant", "Working on it.")
    task_store.add_chat_message("chat-headless", "assistant", "Here is the result.")
    # Interactive: the transcript tailer backfills the same conversation.
    task_store.add_chat_message("chat-interactive", "user", "do the thing")
    task_store.add_chat_message("chat-interactive", "assistant", "Working on it.")
    task_store.add_chat_message("chat-interactive", "assistant", "Here is the result.")

    headless = scheduler._collect_task_output("chat-headless")
    interactive = scheduler._collect_task_output("chat-interactive")
    assert headless == interactive == "Working on it.\n\nHere is the result."
