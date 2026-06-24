"""Operator-forced platform settings.

`config.forced_settings()` pins platform_settings keys on managed/cloud installs.
The enforcement lives in `storage.database`: forced values win on read, writes to
forced keys are no-ops (the operator owns them), and the underlying DB row is left
untouched so removing the force reveals the original value again.
"""

import config
from storage import database as db


def test_read_overlay_wins(temp_db, monkeypatch):
    db.set_platform_setting("session_timeout", "3600")
    assert db.get_platform_setting("session_timeout") == "3600"

    monkeypatch.setattr(config, "forced_settings", lambda: {"session_timeout": "9999"})
    assert db.get_platform_setting("session_timeout") == "9999"


def test_write_to_forced_key_is_noop_and_preserves_db(temp_db, monkeypatch):
    db.set_platform_setting("session_retention_days", "180")
    monkeypatch.setattr(config, "forced_settings", lambda: {"session_retention_days": "30"})

    db.set_platform_setting("session_retention_days", "7")  # ignored — forced
    assert db.get_platform_setting("session_retention_days") == "30"

    # Underlying DB row is untouched: dropping the force reveals the original.
    monkeypatch.setattr(config, "forced_settings", lambda: {})
    assert db.get_platform_setting("session_retention_days") == "180"


def test_get_all_overlays_forced(temp_db, monkeypatch):
    db.set_platform_setting("smtp_host", "db.example.com")
    monkeypatch.setattr(config, "forced_settings", lambda: {"smtp_host": "forced.example.com", "extra": "v"})
    alls = db.get_all_platform_settings()
    assert alls["smtp_host"] == "forced.example.com"
    assert alls["extra"] == "v"


def test_parse_is_failsafe():
    assert config._parse_forced_settings("") == {}
    assert config._parse_forced_settings("   ") == {}
    assert config._parse_forced_settings("{not json") == {}
    assert config._parse_forced_settings("[1, 2]") == {}          # non-object
    assert config._parse_forced_settings('"a string"') == {}      # non-object
    assert config._parse_forced_settings('{"a": true, "b": false, "c": 180}') == {
        "a": "1", "b": "", "c": "180",
    }
