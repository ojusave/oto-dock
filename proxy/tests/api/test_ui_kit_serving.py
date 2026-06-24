"""Dashboard SPA catch-all: /ui-kit/* serving contract.

/ui-kit/* files are the only subresources sandboxed display_ui artifact
iframes may load (kit JS/CSS/fonts, built into dashboard/dist/ui-kit/).
Unlike every other dist path, a MISS must be a loud 404 — the generic SPA
fallback would serve index.html with HTTP 200, and a <script src> pointing at
a mis-copied kit file would then silently load HTML-as-JS (SyntaxError, no
signal). These tests pin that guard plus the untouched fallback behavior.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def dist(tmp_path, monkeypatch):
    """A fake dashboard dist tree with a kit file, wired into config."""
    import config

    (tmp_path / "index.html").write_text("<!doctype html><title>spa</title>")
    # app.py mounts /assets from DASHBOARD_DIST at import time (check_dir)
    (tmp_path / "assets").mkdir()
    kit = tmp_path / "ui-kit"
    (kit / "fonts").mkdir(parents=True)
    (kit / "otodock-tokens.css").write_text(":root { --p-bg: #FAF9F9; }")
    (kit / "fonts" / "comfortaa-latin-400-normal.woff2").write_bytes(b"wOF2fake")
    monkeypatch.setattr(config, "DASHBOARD_ENABLED", True)
    monkeypatch.setattr(config, "DASHBOARD_DIST", tmp_path)
    return tmp_path


@pytest.fixture
def client(dist):
    from app import app

    return TestClient(app)


def test_ui_kit_real_file_served(client):
    r = client.get("/ui-kit/otodock-tokens.css")
    assert r.status_code == 200
    assert r.text == ":root { --p-bg: #FAF9F9; }"
    assert r.headers["content-type"].startswith("text/css")
    # Artifact iframes fetch from an OPAQUE origin, and @font-face requests
    # are CORS-mode — kit files must carry ACAO or fonts silently fall back.
    assert r.headers["access-control-allow-origin"] == "*"


def test_ui_kit_nested_file_served(client):
    r = client.get("/ui-kit/fonts/comfortaa-latin-400-normal.woff2")
    assert r.status_code == 200
    assert r.content == b"wOF2fake"
    assert r.headers["access-control-allow-origin"] == "*"


def test_non_kit_dist_files_get_no_cors_header(client):
    r = client.get("/index.html")
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_ui_kit_miss_is_404_not_index(client):
    r = client.get("/ui-kit/echarts.min.js")
    assert r.status_code == 404
    assert "spa" not in r.text  # never the index.html fallback


def test_ui_kit_traversal_escape_is_404(client):
    # uvicorn percent-decodes scope['path'] before routing, so dot-segments
    # reach the handler literally. A path escaping dist resolves to None in
    # _safe_dashboard_file and must hit the ui-kit 404 guard — without it,
    # this fell through to index.html with 200.
    r = client.get("/ui-kit/%2e%2e/%2e%2e/whatever.js")
    assert r.status_code == 404


def test_spa_fallback_unaffected(client):
    r = client.get("/chat/some-agent/some-chat-id")
    assert r.status_code == 200
    assert "spa" in r.text
    assert "no-store" in r.headers["cache-control"]
