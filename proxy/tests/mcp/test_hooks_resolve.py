"""Regression tests for `api/hooks._resolve_hook_path` path-form contract.

The MCP framework v2 cleanup changed file-tools to post agents-relative paths
to `/v1/hooks/document-preview`, `/v1/hooks/file`, and `/v1/hooks/file-written`
instead of host-absolute. The resolver must accept all three documented forms:

  1. Real host-absolute (legacy)
  2. Agents-relative (canonical for Docker MCPs post-v2)
  3. Sandbox-virtual (canonical for stdio MCPs with OTO_* env)
"""

from pathlib import Path

import pytest

import config
from api.hooks import hooks


@pytest.fixture
def tmp_agents_dir(tmp_path, monkeypatch):
    """Redirect AGENTS_DIR to a temp tree with a fake agent + workspace file."""
    agents = tmp_path / "agents"
    workspace = agents / "personal-assistant" / "users" / "alice" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "report.docx").write_text("dummy")
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    return agents


def test_form1_real_host_path_returned(tmp_agents_dir):
    """Form 1: real host path should be returned as-is."""
    real = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    resolved = hooks._resolve_hook_path("session-x", str(real))
    assert resolved == real


def test_form2_agents_relative_resolves_under_agents_dir(tmp_agents_dir):
    """Form 2: agents-relative path (post-v2 canonical for Docker MCPs)."""
    resolved = hooks._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/report.docx",
    )
    expected = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved == expected
    assert resolved.is_file()


def test_form2_agents_relative_missing_file_falls_through(tmp_agents_dir):
    """Form 2 with a missing file should NOT match — caller will 404."""
    resolved = hooks._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/nonexistent.docx",
    )
    # Falls through; not a real file → caller's .is_file() check raises 404.
    assert not resolved.is_file()


def test_form2_does_not_double_agent_dir(tmp_agents_dir):
    """Form 2 must NOT prepend the agent dir twice (was the original bug).

    Before the fix, agents-relative paths fell through to `_sandbox_to_host`'s
    fallback which returned `<agent_dir>/<input>`, producing
    `<AGENTS_DIR>/personal-assistant/personal-assistant/users/...` — file
    not found, /v1/hooks/document-preview returned 400.
    """
    # Simulate a session ctx for the agent so _sandbox_to_host would be invoked
    # if form 2 wasn't handled first.
    resolved = hooks._resolve_hook_path(
        "session-x",
        "personal-assistant/users/alice/workspace/report.docx",
    )
    expected = tmp_agents_dir / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved == expected
    # The bug would have produced this path:
    bug_path = tmp_agents_dir / "personal-assistant" / "personal-assistant" / "users" / "alice" / "workspace" / "report.docx"
    assert resolved != bug_path


def test_to_agents_relative_strips_prefix(tmp_agents_dir):
    """`_to_agents_relative` strips the AGENTS_DIR prefix to produce form 2."""
    host = str(tmp_agents_dir) + "/personal-assistant/users/alice/workspace/report.docx"
    rel = hooks._to_agents_relative(host)
    assert rel == "/personal-assistant/users/alice/workspace/report.docx"
