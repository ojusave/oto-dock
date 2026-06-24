"""Server-kick rescue at WS close (ws/dashboard._extract_server_kicks).

A chat's server-owned first turn rides the per-connection notify queue; the
queue dies with the connection, so a kick waiting behind a streaming turn was
silently lost on refresh/blip ("my first message never got answered"). The
close handler now drains the queue through this helper and runs every rescued
kick headless. These tests lock the extraction semantics: only `_server_kick`
items are kept (in order), everything else is dropped, the queue ends empty.
"""

import asyncio

from ws.dashboard import _extract_server_kicks


def _kick(cid: str) -> dict:
    return {"type": "_server_kick", "chat_id": cid, "session_id": f"s-{cid}",
            "text": "hello", "images": [], "files": []}


def test_extracts_only_kicks_in_order():
    q: asyncio.Queue = asyncio.Queue()
    q.put_nowait({"type": "notification", "data": {}})
    q.put_nowait(_kick("c1"))
    q.put_nowait({"type": "bg_nudge", "chat_id": "x"})
    q.put_nowait(_kick("c2"))
    q.put_nowait("garbage-non-dict")

    kicks = _extract_server_kicks(q)

    assert [k["chat_id"] for k in kicks] == ["c1", "c2"]
    assert all(k["type"] == "_server_kick" for k in kicks)
    assert q.empty()  # non-kick items dropped, exactly as a dead queue did


def test_empty_queue():
    q: asyncio.Queue = asyncio.Queue()
    assert _extract_server_kicks(q) == []
    assert q.empty()


def test_kick_payload_preserved():
    q: asyncio.Queue = asyncio.Queue()
    payload = {"type": "_server_kick", "chat_id": "c9", "session_id": "s9",
               "text": "first prompt", "images": [{"x": 1}], "files": [{"f": 2}]}
    q.put_nowait(payload)
    kicks = _extract_server_kicks(q)
    assert kicks == [payload]
