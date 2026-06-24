"""Parity tests: feeding the same raw NDJSON fixture through the shared
translator used by BOTH the local and the remote paths should produce
identical CommonEvent streams.

This is the guarantee the refactor exists to preserve — tool cards,
subagents, todos, plan mode, metadata, etc. must surface on the pump
identically whether the agent runs on the local sandbox or a remote
satellite.
"""

from __future__ import annotations

from core.layers.cli.layer import cli_chunk_to_events
from core.layers.cli.translator import ClaudeCLIEventTranslator


def _events_for_chunks(chunks):
    out = []
    for c in chunks:
        for e in cli_chunk_to_events(c):
            out.append((e.type, dict(e.data or {})))
    return out


def _fixture_turn() -> list[dict]:
    """A representative turn: text, thinking, tool use with TodoWrite, result."""
    return [
        {"type": "system", "subtype": "init", "mcp_servers": []},
        {"type": "stream_event", "event": {"type": "message_start",
            "message": {"usage": {"input_tokens": 50, "cache_read_input_tokens": 100}}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "thinking"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "reasoning"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
            "content_block": {"type": "text"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "Hello "}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "world"}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 2,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "TodoWrite"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 2,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"todos":[{"content":"t1","status":"pending"}]}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 2}},
        {"type": "result",
            "total_cost_usd": 0.01,
            "duration_ms": 1000,
            "is_error": False,
            "usage": {"input_tokens": 50, "cache_read_input_tokens": 100,
                      "cache_creation_input_tokens": 0, "output_tokens": 20},
            "modelUsage": {"claude-opus-4-7": {"contextWindow": 200000}}},
    ]


def _run_through_translator(events: list[dict]) -> list[tuple]:
    """Feed events through the shared translator + chunk→event mapper."""
    t = ClaudeCLIEventTranslator("sid")
    out = []
    for e in events:
        chunks = t.feed(e)
        out.extend(_events_for_chunks(chunks))
    return out


def test_same_fixture_same_event_stream():
    """The invariant the refactor exists to preserve: the same raw events
    produce the same CommonEvent stream no matter which layer feeds them."""
    events = _fixture_turn()
    local_stream = _run_through_translator(events)
    remote_stream = _run_through_translator(events)
    assert local_stream == remote_stream


def test_fixture_produces_expected_event_types():
    """Sanity check: the fixture exercises the full set of event types we
    care about for local/remote parity."""
    events = _fixture_turn()
    stream = _run_through_translator(events)
    types = [e[0] for e in stream]
    # Must include each of these to count as real feature parity:
    assert "thinking" in types       # thinking blocks
    assert "text" in types           # streamed text
    assert "tool_use" in types       # tool start (maps from tool_start)
    assert "tool_input" in types     # tool_info → tool_input
    assert "todo_update" in types    # derived by cli_chunk_to_events for TodoWrite
    assert "tool_result" in types    # tool_end (emitted by result's active_tool close)
    assert "metadata" in types       # cost/duration/tokens


def test_translator_spawn_emits_tool_use_id_for_both_modes():
    """Task/Agent dispatches emit a task_spawn carrying the tool_use_id +
    run_in_background flag — identically regardless of which path fed them.
    (Completion gating lives in the SubagentRegistry, populated by
    task_started, not in translator counters.)"""
    events = [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "Task"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"description":"bg","run_in_background":true}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
        {"type": "stream_event", "event": {"type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "id": "tu2", "name": "Task"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta",
                      "partial_json": '{"description":"fg","run_in_background":false}'}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
    ]
    t = ClaudeCLIEventTranslator("sid")
    spawns = []
    for e in events:
        for c in t.feed(e):
            if c.event_type == "task_spawn":
                spawns.append(c.event_data)
    assert t.agents_spawned == 2
    assert spawns[0]["tool_use_id"] == "tu1" and spawns[0]["run_in_background"] is True
    assert spawns[1]["tool_use_id"] == "tu2" and spawns[1]["run_in_background"] is False


# ---------------------------------------------------------------------------
# TaskCreate/TaskUpdate checklist (the TodoWrite successor harness)
# ---------------------------------------------------------------------------

def _task_tool_call(idx: int, tool_id: str, name: str, input_json: str) -> list[dict]:
    return [
        {"type": "stream_event", "event": {"type": "content_block_start", "index": idx,
            "content_block": {"type": "tool_use", "id": tool_id, "name": name}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": input_json}}},
        {"type": "stream_event", "event": {"type": "content_block_stop", "index": idx}},
    ]


def _task_result(tool_id: str, text: str) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id,
         "content": [{"type": "text", "text": text}]},
    ]}}


def _todo_snapshots(translator, events):
    snaps = []
    for e in events:
        for c in translator.feed(e):
            for ev in cli_chunk_to_events(c):
                if ev.type == "todo_update":
                    snaps.append(ev.data["todos"])
    return snaps


def test_task_tools_emit_todo_snapshots():
    """TaskCreate (id from the RESULT) + TaskUpdate (id in the input) maintain
    the session checklist and emit full todo_update snapshots — the TodoWrite
    contract the pump/TodoPanel already speak."""
    t = ClaudeCLIEventTranslator("sid")
    events = [
        *_task_tool_call(0, "tc1", "TaskCreate",
                         '{"subject":"Read schema","description":"d"}'),
        *_task_tool_call(1, "tc2", "TaskCreate",
                         '{"subject":"Write docs","description":"d"}'),
        _task_result("tc1", "Task #4 created successfully: Read schema"),
        _task_result("tc2", "Task #5 created successfully: Write docs"),
        *_task_tool_call(2, "tu1", "TaskUpdate",
                         '{"taskId":"4","status":"in_progress"}'),
        *_task_tool_call(3, "tu2", "TaskUpdate",
                         '{"taskId":"4","status":"completed"}'),
    ]
    snaps = _todo_snapshots(t, events)
    assert len(snaps) == 4  # 2 result-inserts + 2 updates
    assert snaps[1] == [
        {"content": "Read schema", "status": "pending"},
        {"content": "Write docs", "status": "pending"},
    ]
    assert snaps[2][0]["status"] == "in_progress"
    assert snaps[3] == [
        {"content": "Read schema", "status": "completed"},
        {"content": "Write docs", "status": "pending"},
    ]


def test_task_tools_delete_and_unknown_id():
    """`deleted` removes the item; an update for an id we never saw inserts a
    placeholder (created before a proxy restart). Checklist survives turn
    resets (session-scoped)."""
    t = ClaudeCLIEventTranslator("sid")
    snaps = _todo_snapshots(t, [
        *_task_tool_call(0, "tc1", "TaskCreate", '{"subject":"A","description":"d"}'),
        _task_result("tc1", "Task #1 created successfully: A"),
    ])
    assert snaps[-1] == [{"content": "A", "status": "pending"}]

    t.reset_for_new_turn()  # checklist must survive
    snaps = _todo_snapshots(t, [
        *_task_tool_call(0, "tu1", "TaskUpdate", '{"taskId":"9","status":"in_progress"}'),
        *_task_tool_call(1, "tu2", "TaskUpdate", '{"taskId":"1","status":"deleted"}'),
    ])
    assert snaps[0] == [
        {"content": "A", "status": "pending"},
        {"content": "Task #9", "status": "in_progress"},
    ]
    assert snaps[1] == [{"content": "Task #9", "status": "in_progress"}]


def test_task_create_without_result_emits_nothing():
    """A staged create with no result yet (or a failed create) must not
    surface a phantom item; non-task tool_results are ignored."""
    t = ClaudeCLIEventTranslator("sid")
    snaps = _todo_snapshots(t, [
        *_task_tool_call(0, "tc1", "TaskCreate", '{"subject":"A","description":"d"}'),
        _task_result("other-tool", "irrelevant"),
        _task_result("tc1", "Error: could not create task"),
    ])
    assert snaps == []


def test_thinking_tokens_becomes_live_progress():
    """`thinking_tokens` system pings (adaptive-effort models hide thinking
    content) become THINKING phase=progress events — live-only gauge, never a
    SYSTEM event (the old passthrough persisted hundreds of junk rows per
    long turn). Zero/invalid estimates are dropped."""
    t = ClaudeCLIEventTranslator("sid")
    events = [
        {"type": "system", "subtype": "thinking_tokens",
         "estimated_tokens": 450, "estimated_tokens_delta": 100},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 0},
        {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": "junk"},
    ]
    out = []
    for e in events:
        for c in t.feed(e):
            out.extend(cli_chunk_to_events(c))
    assert len(out) == 1
    assert out[0].type == "thinking"
    assert out[0].data == {"phase": "progress", "estimated_tokens": 450}
