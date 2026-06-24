"""The PostToolUse forwarder must ship the RESULT BODY, not an empty string.

Live-found on T1 (2026-07-08, session N): the hook read ``inp["tool_result"]``
but the CLI's PostToolUse input carries the result under ``tool_response``
(CLI 2.1.201) — so every headless Claude tool pill shipped an empty
``result_content`` ("ok" summaries, no Output section) while codex normal
chats and interactive transcripts showed full output. These tests lock the
key fallback and the stdout+stderr extraction.
"""

from __future__ import annotations

import importlib.util
import io
import json

from tests._paths import PROXY_DIR

_FWD = PROXY_DIR / "hooks" / "tool_result_forwarder.py"
_spec = importlib.util.spec_from_file_location("tool_result_forwarder_body", _FWD)
forwarder = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(forwarder)


class TestExtractResultText:
    def test_bash_stdout(self):
        assert forwarder._extract_result_text(
            {"stdout": "alpha\nbeta\n", "stderr": ""}
        ) == "alpha\nbeta\n"

    def test_bash_stdout_plus_stderr(self):
        # codex's aggregatedOutput interleaves both streams — keep the Claude
        # pills comparable.
        assert forwarder._extract_result_text(
            {"stdout": "alpha\n", "stderr": "oops\n"}
        ) == "alpha\noops\n"

    def test_stderr_only(self):
        assert forwarder._extract_result_text(
            {"stdout": "", "stderr": "boom\n"}
        ) == "boom\n"

    def test_content_blocks(self):
        assert forwarder._extract_result_text(
            {"content": [{"type": "text", "text": "hello"},
                         {"type": "image", "data": "…"}]}
        ) == "hello"

    def test_read_file_content(self):
        # Read responses nest the body under file.content — without the
        # fallback every Read pill summarized as "empty file" (2026-07-11).
        assert forwarder._extract_result_text(
            {"type": "text", "file": {"filePath": "/w/x.html",
                                      "content": "<div>hi</div>",
                                      "numLines": 1}}
        ) == "<div>hi</div>"
        assert forwarder._extract_summary(
            "Read", {}, "<div>hi</div>\nline2") == "2 lines"

    def test_plain_string(self):
        assert forwarder._extract_result_text("just text") == "just text"


def _run_main(monkeypatch, hook_input):
    """Drive main() with a fake stdin/env and capture the POSTed payload."""
    posted = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        posted.append(json.loads(req.data.decode()))
        return _Resp()

    monkeypatch.setattr(forwarder.sys, "stdin", io.StringIO(json.dumps(hook_input)))
    monkeypatch.setattr(forwarder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    monkeypatch.setenv("OTO_SESSION_ID", "sess-hook-test")
    monkeypatch.setenv("PROXY_URL", "http://proxy.test")
    monkeypatch.setenv("PROXY_API_KEY", "k")
    forwarder.main()
    return posted


class TestResponseKeyFallback:
    def test_tool_response_key_ships_the_body(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "Bash",
            "tool_input": {"command": "printf hi"},
            "tool_use_id": "toolu_1",
            "tool_response": {"stdout": "hi\n", "stderr": ""},
        })
        assert len(posted) == 1
        assert posted[0]["result_content"] == "hi\n"
        assert posted[0]["summary"] == "2 lines"
        assert posted[0]["tool_use_id"] == "toolu_1"

    def test_legacy_tool_result_key_still_accepted(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "Bash",
            "tool_input": {"command": "printf hi"},
            "tool_result": {"stdout": "hi\n"},
        })
        assert posted and posted[0]["result_content"] == "hi\n"

    def test_missing_result_still_posts_ok_summary(self, monkeypatch):
        posted = _run_main(monkeypatch, {
            "tool_name": "Bash",
            "tool_input": {"command": "true"},
            "tool_response": {"stdout": "", "stderr": ""},
        })
        assert posted and posted[0]["result_content"] == ""
        assert posted[0]["summary"] == "ok"
