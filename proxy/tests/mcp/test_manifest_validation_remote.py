"""Installer manifest validation — remote (vendor-hosted) MCPs.

``server.source: remote:*`` MCPs (linear/slack/zoom) run nothing locally, so
runtime/command must not be required; a URL + non-stdio transport are what the
connection actually needs. Locally-run MCPs keep the strict runtime checks.
"""

from services.community.community_installer import _validate_manifest


def _base(server: dict) -> dict:
    return {
        "name": "some-mcp", "label": "Some MCP", "description": "d",
        "version": "1.0.0", "category": "community", "server": server,
    }


def test_remote_manifest_valid_without_runtime():
    errors = _validate_manifest(_base({
        "transport": "streamable_http",
        "url_template": "https://mcp.linear.app/mcp",
        "source": "remote:mcp.linear.app",
    }))
    assert errors == []


def test_remote_manifest_requires_url_template():
    errors = _validate_manifest(_base({
        "transport": "streamable_http",
        "source": "remote:mcp.linear.app",
    }))
    assert any("url_template" in e for e in errors)


def test_remote_manifest_rejects_stdio_transport():
    errors = _validate_manifest(_base({
        "url_template": "https://mcp.zoom.us/mcp",
        "source": "remote:mcp.zoom.us",
    }))
    assert any("non-stdio" in e for e in errors)


def test_local_manifest_still_requires_runtime():
    errors = _validate_manifest(_base({
        "transport": "stdio",
        "source": "npm:some-pkg",
    }))
    assert any("server.runtime" in e for e in errors)
