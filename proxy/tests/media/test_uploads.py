"""Tests for `/v1/upload` save-path routing.

Chat-attached files (no
`target_dir`) now land in `users/<u>/workspace/uploads/files/<name>`
instead of workspace root. Workspace-page uploads (with explicit
`target_dir`) are unaffected.

We avoid the full app lifespan (DB schema init, MCP scan, scheduler)
by mounting just `uploads.router` on a minimal FastAPI app and
overriding the auth + helper deps so the test exercises the path
resolution branches without DB/auth ceremony — same approach as
`tests/media/test_collabora_proxy.py`.
"""

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_router(tmp_path, monkeypatch):
    """Mount only the uploads router and stub out auth + agent existence."""
    import config
    from api.media import uploads
    from auth.providers import UserContext

    # Redirect AGENTS_DIR so we don't touch real workspace files
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)
    # `config.get_agent_dir` reads AGENTS_DIR at call-time (it's a function);
    # the helper used by uploads.py:133 is `config.get_agent_dir`. Confirm
    # we're hitting the same function — it is monkeypatched implicitly via
    # the module attribute swap above.

    # Override the auth dependency to return a fixed manager user
    user = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="creator", agents=["test-agent"],
        agent_roles={"test-agent": "manager"},
    )

    async def _stub_user():
        return user

    # Stub agent_store + task_store calls inside the endpoint
    from storage import agent_store
    from storage import database as task_store
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "test-agent")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice" if sub == "user-test-sub" else None)
    # Skip the satellite-push side effect (no remote sessions in tests).
    async def _noop_push(*a, **kw):
        return None
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _noop_push)

    app = FastAPI()
    app.include_router(uploads.router)
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir


# ---------------------------------------------------------------------------
# Behavior — chat upload (no target_dir) lands in uploads/files
# ---------------------------------------------------------------------------


def test_chat_upload_lands_in_uploads_files_default(app_with_router):
    """No `target_dir` → file lands in `users/<u>/workspace/uploads/files/<name>`."""
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("doc.pdf", BytesIO(b"PDF data"), "application/pdf")},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # `path` returned in response is relative to agent_dir (not AGENTS_DIR).
    assert body["path"] == "users/alice/workspace/uploads/files/doc.pdf"
    assert body["filename"] == "doc.pdf"
    # File actually exists on disk in the new subfolder
    expected = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files" / "doc.pdf"
    assert expected.is_file()
    assert expected.read_bytes() == b"PDF data"
    # AND nothing leaked into workspace root
    workspace_root = agents_dir / "test-agent" / "users" / "alice" / "workspace"
    root_files = [p for p in workspace_root.iterdir() if p.is_file()]
    assert root_files == [], f"workspace root should stay clean, found: {root_files}"


# ---------------------------------------------------------------------------
# No extension allowlist — any file type uploads (agents run full dev
# environments; the serving layer forces non-inert types to attachment)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,mime", [
    ("page.html", "text/html"),
    ("art.psd", "image/vnd.adobe.photoshop"),
    ("print.gcode", "text/x.gcode"),
    ("drawing.dwg", "application/acad"),
    ("tool.exe", "application/octet-stream"),
    ("Makefile", "application/octet-stream"),  # extensionless
])
def test_any_file_type_uploads(app_with_router, name, mime):
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": (name, BytesIO(b"payload"), mime)},
        data={"agent": "test-agent"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == f"users/alice/workspace/uploads/files/{name}"


# ---------------------------------------------------------------------------
# Workspace-page regression — explicit target_dir is honored exactly
# ---------------------------------------------------------------------------


def test_workspace_target_dir_unchanged(app_with_router):
    """`target_dir=workspace/research` → file lands at `workspace/research/<name>` exactly.

    Confirms we did not auto-prefix `uploads/files/` onto explicit target_dirs
    — that would break the workspace-page upload UX where the user picks
    their own folder.
    """
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("note.md", BytesIO(b"# notes"), "text/markdown")},
        data={"agent": "test-agent", "target_dir": "workspace/research"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "workspace/research/note.md"
    assert "uploads/files" not in body["path"], "no auto-prefix injection"
    expected = agents_dir / "test-agent" / "workspace" / "research" / "note.md"
    assert expected.is_file()


def test_workspace_user_target_dir_unchanged(app_with_router):
    """`target_dir=users/alice/workspace/portraits` → respected exactly."""
    app, agents_dir = app_with_router
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("avatar.png", BytesIO(b"\x89PNG"), "image/png")},
        data={"agent": "test-agent", "target_dir": "users/alice/workspace/portraits"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["path"] == "users/alice/workspace/portraits/avatar.png"


# ---------------------------------------------------------------------------
# Filename sanitization + conflict resolution still work after the change
# ---------------------------------------------------------------------------


def test_chat_upload_filename_conflict_resolved(app_with_router):
    """Two uploads with the same name produce `<n>` and `<n>_1`."""
    app, agents_dir = app_with_router
    client = TestClient(app)

    for _ in range(2):
        resp = client.post(
            "/v1/upload",
            files={"file": ("notes.md", BytesIO(b"x"), "text/markdown")},
            data={"agent": "test-agent"},
        )
        assert resp.status_code == 200

    uploads_dir = agents_dir / "test-agent" / "users" / "alice" / "workspace" / "uploads" / "files"
    names = sorted(p.name for p in uploads_dir.iterdir())
    assert names == ["notes.md", "notes_1.md"]


# ---------------------------------------------------------------------------
# Agent-scoped uploads (internal-agent chats)
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_internal_agent(tmp_path, monkeypatch):
    """Same fixture as `app_with_router` but the agent is internal (agent-scoped chats)."""
    import config
    from api.media import uploads
    from auth.providers import UserContext

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(config, "AGENTS_DIR", agents_dir)

    user = UserContext(
        sub="user-test-sub", email="alice@test.com", name="Alice",
        role="creator", agents=["internal-bot"],
        agent_roles={"internal-bot": "manager"},
    )

    async def _stub_user():
        return user

    from storage import agent_store
    from storage import database as task_store
    from core.session import visibility as _vis
    monkeypatch.setattr(agent_store, "agent_exists", lambda name: name == "internal-bot")
    monkeypatch.setattr(_vis, "is_shared_only", lambda name: name == "internal-bot")
    monkeypatch.setattr(task_store, "get_username_by_sub", lambda sub: "alice" if sub == "user-test-sub" else None)
    async def _noop_push(*a, **kw):
        return None
    monkeypatch.setattr(uploads, "_push_upload_to_active_remote_sessions", _noop_push)

    app = FastAPI()
    app.include_router(uploads.router)
    from auth.providers import get_current_user
    app.dependency_overrides[get_current_user] = _stub_user
    return app, agents_dir, user


def test_agent_scoped_upload_lands_in_workspace(app_with_internal_agent):
    """Internal-agent chat → file lands in `workspace/uploads/files/<name>`,
    NOT under any user dir. Manager uploading to set up the bot."""
    app, agents_dir, _ = app_with_internal_agent
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("playbook.md", BytesIO(b"# steps"), "text/markdown")},
        data={"agent": "internal-bot"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["path"] == "workspace/uploads/files/playbook.md"
    expected = agents_dir / "internal-bot" / "workspace" / "uploads" / "files" / "playbook.md"
    assert expected.is_file()
    assert expected.read_bytes() == b"# steps"
    # Nothing leaked into a per-user dir
    users_dir = agents_dir / "internal-bot" / "users"
    assert not users_dir.exists() or not any(users_dir.iterdir())


def test_agent_scoped_upload_rejects_viewer(app_with_internal_agent, monkeypatch):
    """Viewers cannot post into an internal agent's shared `/workspace/`.
    Path policy permits the write at the OS level (agent-scoped sessions
    can write `/workspace/`), but for the API caller we add a per-agent
    manager check — defense in depth."""
    app, _, user = app_with_internal_agent
    # Demote: per-agent viewer + platform member (defense in depth)
    user.agent_roles["internal-bot"] = "viewer"
    user.role = "member"
    client = TestClient(app)

    resp = client.post(
        "/v1/upload",
        files={"file": ("playbook.md", BytesIO(b"# steps"), "text/markdown")},
        data={"agent": "internal-bot"},
    )
    assert resp.status_code == 403, resp.text
    assert "manager" in resp.text.lower()
