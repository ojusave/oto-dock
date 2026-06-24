"""Per-provider token directory parametrization.

Each OAuth provider gets its own dir under sessions/{provider_id}-tokens/
so multiple providers' token files don't collide.
"""

from __future__ import annotations

import pytest

from services.oauth import oauth_account_store


def test_get_token_base_dir_uses_provider_id(monkeypatch, tmp_path):
    """sessions/{provider_id}-tokens/ — one root per provider."""
    import config as _config
    monkeypatch.setattr(_config, "SESSIONS_DIR", tmp_path, raising=False)

    base = oauth_account_store.get_token_base_dir("slack")
    assert base == tmp_path / "slack-tokens"
    assert base.is_dir()


def test_two_providers_get_distinct_dirs(monkeypatch, tmp_path):
    """google-tokens and slack-tokens are entirely separate trees."""
    import config as _config
    monkeypatch.setattr(_config, "SESSIONS_DIR", tmp_path, raising=False)

    g_dir = oauth_account_store.get_token_dir("alice", provider_id="google")
    s_dir = oauth_account_store.get_token_dir("alice", provider_id="slack")
    assert g_dir == tmp_path / "google-tokens" / "alice"
    assert s_dir == tmp_path / "slack-tokens" / "alice"
    assert g_dir != s_dir
    assert g_dir.is_dir() and s_dir.is_dir()


def test_user_dir_under_provider_root(monkeypatch, tmp_path):
    """A user's token dir lives under the provider-token root (one dir per
    user — there is no platform service tier)."""
    import config as _config
    monkeypatch.setattr(_config, "SESSIONS_DIR", tmp_path, raising=False)

    user_dir = oauth_account_store.get_token_dir("alice", provider_id="linear")
    assert user_dir == tmp_path / "linear-tokens" / "alice"
    root = tmp_path / "linear-tokens"
    assert root in user_dir.parents


def test_get_token_base_dir_rejects_empty_provider(monkeypatch, tmp_path):
    """Empty / non-string provider_id raises (defensive — would otherwise
    silently create `sessions/-tokens/` which is broken)."""
    import config as _config
    monkeypatch.setattr(_config, "SESSIONS_DIR", tmp_path, raising=False)

    with pytest.raises(ValueError, match="provider_id"):
        oauth_account_store.get_token_base_dir("")


def test_get_token_dir_requires_username(monkeypatch, tmp_path):
    """An empty username raises (every token dir is user-scoped now)."""
    import config as _config
    monkeypatch.setattr(_config, "SESSIONS_DIR", tmp_path, raising=False)

    with pytest.raises(ValueError, match="username is required"):
        oauth_account_store.get_token_dir("", provider_id="slack")
