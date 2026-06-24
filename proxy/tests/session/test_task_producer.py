"""task_produce bg-work review-turn decision.

The producer must send the "review the output and continue" nudge only for
background work the model has NOT seen: pending commands/subagents, or bash
completions resolved after the model's final message (settle drain / post-turn
monitor). Bash completions surfaced inline during generation (the CLI injects
the task-notification into the live turn) must NOT trigger an extra turn —
regression for a 39-command turn that ended fully-reviewed and still got the
nudge. Background SUBAGENTS keep the spawn-tally contract unchanged.
"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from core.events.common_events import (
    CommonEvent, SUBAGENT_START, BG_COMMAND_START, QUEUE_TURN,
)
from core.events.bg_command_state import (
    _bg_command_registries, get_bg_command_registry,
)
from core.events.task_producer import task_produce
from core.session.session_state import get_subagent_registry


class _FakeLayer:
    """Minimal ExecutionLayer stand-in: canned event lists per send_message.

    ``on_wait_commands`` lets a test resolve a pending command the way the
    real ``wait_for_bg_commands`` path does (post-turn drain → unsurfaced).
    """

    def __init__(self, turns, on_wait_commands=None):
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.on_wait_commands = on_wait_commands

    @asynccontextmanager
    async def session_lock(self, session_id):
        yield

    async def send_message(self, session_id, prompt, **kw):
        self.prompts.append(prompt)
        for event in (self.turns.pop(0) if self.turns else []):
            yield event

    async def wait_for_bg_subagents(self, session_id, timeout=120.0):
        return True

    async def wait_for_bg_commands(self, session_id, timeout=120.0):
        if self.on_wait_commands is not None:
            self.on_wait_commands()
        return True


def _bg_cmd_start():
    return CommonEvent(type=BG_COMMAND_START, data={})


def _bg_sub_start():
    return CommonEvent(type=SUBAGENT_START, data={"run_in_background": True})


async def _run(layer, session_id):
    queue: asyncio.Queue = asyncio.Queue()
    await task_produce(layer, session_id, "do the task", queue, "run12345")
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.fixture
def clean_registries():
    """Unique-session tests still clean up so nothing leaks across runs."""
    created: list[str] = []
    yield created
    for sid in created:
        _bg_command_registries.pop(sid, None)
        reg = get_subagent_registry(sid)
        reg.spawned.clear()
        reg.completed.clear()


@pytest.mark.asyncio
async def test_all_commands_surfaced_inline_skips_nudge(clean_registries):
    """Every bash completion was read by the model during the turn → the
    producer must return after the main turn, no nudge."""
    sid = "tp-surfaced"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")
    bgreg.register_spawn("cmd-b", "t2")
    bgreg.mark_done("cmd-a")  # surfaced (default) — mid-generation resolve
    bgreg.mark_done("cmd-b")

    layer = _FakeLayer(turns=[[_bg_cmd_start(), _bg_cmd_start()]])
    events = await _run(layer, sid)

    assert layer.prompts == ["do the task"]
    assert not [e for e in events if e.type == QUEUE_TURN]


@pytest.mark.asyncio
async def test_settle_resolved_command_still_nudges(clean_registries):
    """A completion the model never saw (settle/post-turn resolve) keeps the
    delegation contract: exactly one review turn."""
    sid = "tp-unsurfaced"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")
    bgreg.mark_done("cmd-a", surfaced=False)

    layer = _FakeLayer(turns=[[_bg_cmd_start()], []])
    events = await _run(layer, sid)

    assert len(layer.prompts) == 2
    assert "1 background command(s)" in layer.prompts[1]
    assert len([e for e in events if e.type == QUEUE_TURN]) == 1


@pytest.mark.asyncio
async def test_pending_command_waits_then_nudges(clean_registries):
    """Still-running command at turn end: the producer waits (the drain
    resolves it unsurfaced) and then sends the review turn."""
    sid = "tp-pending"
    clean_registries.append(sid)
    bgreg = get_bg_command_registry(sid)
    bgreg.register_spawn("cmd-a", "t1")

    layer = _FakeLayer(
        turns=[[_bg_cmd_start()], []],
        on_wait_commands=lambda: bgreg.mark_done("cmd-a", surfaced=False),
    )
    events = await _run(layer, sid)

    assert len(layer.prompts) == 2
    assert len([e for e in events if e.type == QUEUE_TURN]) == 1


@pytest.mark.asyncio
async def test_bg_subagent_spawn_tally_still_nudges(clean_registries):
    """Background SUBAGENTS keep the spawn-tally contract: a bg spawn forces
    the review turn even with both registries drained."""
    sid = "tp-subagent"
    clean_registries.append(sid)

    layer = _FakeLayer(turns=[[_bg_sub_start()], []])
    events = await _run(layer, sid)

    assert len(layer.prompts) == 2
    assert "1 background agent(s)" in layer.prompts[1]
    # Bash didn't participate — the nudge must not mention commands.
    assert "command(s)" not in layer.prompts[1]
    assert len([e for e in events if e.type == QUEUE_TURN]) == 1


@pytest.mark.asyncio
async def test_no_bg_work_no_nudge(clean_registries):
    sid = "tp-none"
    clean_registries.append(sid)
    layer = _FakeLayer(turns=[[]])
    events = await _run(layer, sid)
    assert layer.prompts == ["do the task"]
    assert not [e for e in events if e.type == QUEUE_TURN]
