"""The /v1/phone/calls relay: auth, the phone-mcp assignment gate, forwarding.

phone-mcp reaches the phone daemon THROUGH these endpoints (session JWT over
PROXY_URL — loopback locally, the satellite tunnel remotely), so a session
machine never needs daemon reachability and PHONE_API_SECRET stays
proxy-side. A bare session JWT must not grant calling: the relay requires
phone-mcp among the calling agent's enabled MCPs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import config
from api.phone import phone_relay
from auth.session_token import create_session_token


class _FakeResponse:
    def __init__(self, status_code=200, content=b'{"ok": true}',
                 content_type="application/json"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _FakeAsyncClient:
    """Captures the forwarded request; returns a canned daemon response."""

    calls: list[dict] = []
    response = _FakeResponse()

    def __init__(self, timeout=None):
        self._timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, params=None, json=None, headers=None):
        _FakeAsyncClient.calls.append({
            "method": method, "url": url, "params": params,
            "json": json, "headers": headers, "timeout": self._timeout,
        })
        return _FakeAsyncClient.response


@pytest.fixture
def fake_daemon(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse()
    monkeypatch.setattr(phone_relay.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://phone-daemon:9093")
    monkeypatch.setattr(config, "PHONE_API_SECRET", "tel-secret")
    return _FakeAsyncClient


def _assigned(monkeypatch, names):
    from services.mcp import mcp_registry
    monkeypatch.setattr(
        mcp_registry, "get_agent_mcps",
        lambda agent, **kw: [SimpleNamespace(name=n) for n in names],
    )


def test_relay_requires_auth():
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent(None))
    assert e.value.status_code == 401

    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent("Bearer not-a-token"))
    assert e.value.status_code == 401


def test_relay_requires_phone_mcp_assignment(monkeypatch, temp_db):
    _assigned(monkeypatch, ["notifications-mcp"])
    token = create_session_token("sid-1", "some-agent", "user-1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._require_phone_agent(f"Bearer {token}"))
    assert e.value.status_code == 403

    _assigned(monkeypatch, ["notifications-mcp", "phone-mcp"])
    asyncio.run(phone_relay._require_phone_agent(f"Bearer {token}"))  # no raise


def test_relay_master_key_passes_without_assignment(monkeypatch):
    monkeypatch.setattr(config, "is_master_key", lambda t: t == "master")
    asyncio.run(phone_relay._require_phone_agent("Bearer master"))  # no raise


def test_relay_forwards_with_daemon_secret(fake_daemon):
    resp = asyncio.run(phone_relay._relay(
        "POST", "/api/calls",
        json_body={"phone_number": "+301234", "task_description": "book"},
    ))
    assert resp.status_code == 200
    call = fake_daemon.calls[0]
    assert call["url"] == "http://phone-daemon:9093/api/calls"
    assert call["json"]["phone_number"] == "+301234"
    # PHONE_API_SECRET attached proxy-side — never travels with the MCP
    assert call["headers"]["Authorization"] == "Bearer tel-secret"


def test_relay_wait_extends_read_timeout(fake_daemon):
    asyncio.run(phone_relay._relay(
        "GET", "/api/calls/c1/wait", params={"timeout": "120"},
        read_timeout=150.0,
    ))
    assert fake_daemon.calls[0]["timeout"].read == 150.0


def test_relay_daemon_unreachable_maps_to_502(monkeypatch):
    import httpx as _httpx

    class _Boom(_FakeAsyncClient):
        async def request(self, *a, **kw):
            raise _httpx.ConnectError("getaddrinfo failed")

    monkeypatch.setattr(phone_relay.httpx, "AsyncClient", _Boom)
    monkeypatch.setattr(config, "PHONE_SERVER_URL", "http://phone-daemon:9093")
    with pytest.raises(HTTPException) as e:
        asyncio.run(phone_relay._relay("GET", "/api/calls/c1"))
    assert e.value.status_code == 502
    assert "phone-daemon:9093" in e.value.detail


def test_relay_passes_daemon_status_through(fake_daemon):
    fake_daemon.response = _FakeResponse(status_code=404, content=b'{"error": "no call"}')
    resp = asyncio.run(phone_relay._relay("GET", "/api/calls/nope"))
    assert resp.status_code == 404
    assert b"no call" in resp.body
