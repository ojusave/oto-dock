"""generic_oauth_v1 token format — writer + reader + aliases.

The canonical OAuth token shape, with optional manifest-declared alias
keys emitted alongside canonical keys for legacy-reader MCPs (workspace-
mcp's google.auth library expects `token` / `token_uri` / `expiry`).
"""

from __future__ import annotations

import json

from services.oauth import oauth_account_store


def test_writer_emits_canonical_keys(tmp_path):
    """generic_oauth_v1 writer produces canonical keys including ISO-8601
    `expires_at` with Z suffix and an `extra` block."""
    token_path = tmp_path / "alice.json"
    oauth_account_store._write_generic_oauth_v1_token(
        token_path,
        provider_id="slack",
        account_id="alice@example.com",
        access_token="xoxb-test",
        refresh_token="rt-test",
        expires_in=3600,
        scopes=["channels:read", "chat:write"],
        client_id="cid",
        client_secret="csec",
        token_url="https://slack.com/api/oauth.v2.access",
        extra={"team_id": "T0123", "user_token": "xoxp-user"},
    )
    payload = json.loads(token_path.read_text())
    assert payload["provider"] == "slack"
    assert payload["account_id"] == "alice@example.com"
    assert payload["access_token"] == "xoxb-test"
    assert payload["refresh_token"] == "rt-test"
    assert payload["token_url"].endswith("/oauth.v2.access")
    assert payload["scopes"] == ["channels:read", "chat:write"]
    assert payload["expires_at"].endswith("Z")
    assert payload["extra"]["team_id"] == "T0123"
    assert payload["extra"]["user_token"] == "xoxp-user"


def test_writer_emits_aliases_dual_shape(tmp_path):
    """When manifest declares aliases, the writer ALSO emits the alias
    keys (e.g., workspace-mcp's `token`/`expiry` for google.auth)."""
    token_path = tmp_path / "work.json"
    oauth_account_store._write_generic_oauth_v1_token(
        token_path,
        provider_id="google",
        account_id="work@example.com",
        access_token="ya29.abc",
        refresh_token="1//rt",
        expires_in=3600,
        scopes=["openid"],
        client_id="g-cid",
        client_secret="g-csec",
        token_url="https://oauth2.googleapis.com/token",
        aliases={
            "token": "access_token",
            "token_uri": "token_url",
            "expiry": "expires_at",
        },
    )
    payload = json.loads(token_path.read_text())
    # Canonical
    assert payload["access_token"] == "ya29.abc"
    assert payload["token_url"] == "https://oauth2.googleapis.com/token"
    # Aliases
    assert payload["token"] == "ya29.abc"
    assert payload["token_uri"] == "https://oauth2.googleapis.com/token"
    # Special case: `expiry` alias uses naive ISO (no Z), canonical uses Z.
    assert "Z" in payload["expires_at"]
    assert "Z" not in payload["expiry"]
    assert "T" in payload["expiry"]


def test_reader_returns_dict_for_valid_file(tmp_path):
    """_read_oauth_token returns the parsed dict for a generic_oauth_v1 file."""
    p = tmp_path / "t.json"
    oauth_account_store._write_generic_oauth_v1_token(
        p,
        provider_id="linear",
        account_id="a@b",
        access_token="t",
        refresh_token="r",
        expires_in=3600,
        scopes=[],
        client_id="c",
        client_secret="s",
        token_url="https://x/t",
    )
    raw = oauth_account_store._read_oauth_token(p)
    assert raw is not None
    assert raw["provider"] == "linear"


def test_reader_returns_none_for_missing_file(tmp_path):
    raw = oauth_account_store._read_oauth_token(tmp_path / "nope.json")
    assert raw is None


def test_canonical_access_token_helper(tmp_path):
    """get_canonical_access_token extracts the access_token field."""
    p = tmp_path / "t.json"
    oauth_account_store._write_generic_oauth_v1_token(
        p,
        provider_id="slack",
        account_id="x",
        access_token="t-canonical",
        refresh_token="r",
        expires_in=3600,
        scopes=[],
        client_id="c",
        client_secret="s",
        token_url="https://x/t",
    )
    raw = oauth_account_store._read_oauth_token(p)
    assert oauth_account_store.get_canonical_access_token(raw) == "t-canonical"


def test_extra_preserved_during_refresh_dispatch(tmp_path):
    """When generic_oauth_v1 file has an extra dict, persisting via write
    keeps the extras intact."""
    token_path = tmp_path / "x.json"
    oauth_account_store._write_generic_oauth_v1_token(
        token_path,
        provider_id="slack",
        account_id="alice",
        access_token="xoxb-1",
        refresh_token="rt",
        expires_in=3600,
        scopes=[],
        client_id="c",
        client_secret="s",
        token_url="https://x/t",
        extra={"team_id": "T1", "preferred_bearer": "user_token", "user_token": "xoxp-u"},
    )
    raw = oauth_account_store._read_oauth_token(token_path)
    assert raw["extra"]["team_id"] == "T1"
    assert raw["extra"]["preferred_bearer"] == "user_token"
    assert raw["extra"]["user_token"] == "xoxp-u"


def test_writer_zero_expiry_emits_empty_sentinel(tmp_path):
    """expires_in <= 0 = the vendor never expires this token: the writer must
    emit the empty-string sentinel the refresh worker treats as never-expires
    (and the same for an expires_at alias) — not a fabricated +1h stamp."""
    token_path = tmp_path / "alice.json"
    oauth_account_store._write_generic_oauth_v1_token(
        token_path,
        provider_id="slack",
        account_id="alice@example.com",
        access_token="xoxp-test",
        refresh_token="",
        expires_in=0,
        scopes=["chat:write"],
        client_id="",
        client_secret="",
        token_url="https://slack.com/api/oauth.v2.user.access",
        extra={"via_relay": True},
        aliases={"expiry": "expires_at"},
    )
    payload = json.loads(token_path.read_text())
    assert payload["expires_at"] == ""
    assert payload["expiry"] == ""
    assert payload["access_token"] == "xoxp-test"
