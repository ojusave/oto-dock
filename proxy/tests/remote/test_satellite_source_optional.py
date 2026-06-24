"""The proxy degrades gracefully when the satellite source tree is absent.

Public builds ship without ``satellite/`` (the remote-machines feature is
staged out of the first OSS release). The version read must return ``None``
instead of raising, ``satellite_source_available()`` must flip the feature
off, and the pairing/bootstrap/update entry points must refuse with a clear
404 — while a full build (this repo) keeps the strict fail-loud behavior.
"""

import pytest
from fastapi import HTTPException


def test_version_read_returns_none_when_tree_absent(monkeypatch, tmp_path):
    from ws import satellite as ws_sat

    monkeypatch.setattr(
        ws_sat, "_SATELLITE_CONFIG_PY", tmp_path / "satellite" / "config.py",
    )
    assert ws_sat._read_satellite_version_from_source() is None


def test_version_read_still_fails_loudly_on_malformed_source(monkeypatch, tmp_path):
    """Present-but-unparseable stays a hard error — that's the drift guard
    (a wrong 'latest' would break auto-update), not a satellite-free build."""
    from ws import satellite as ws_sat

    cfg = tmp_path / "config.py"
    cfg.write_text("VERSION = 'not-the-constant'\n", encoding="utf-8")
    monkeypatch.setattr(ws_sat, "_SATELLITE_CONFIG_PY", cfg)
    with pytest.raises(RuntimeError):
        ws_sat._read_satellite_version_from_source()


def test_version_read_parses_real_tree():
    """A full build resolves a version and reports available; on a
    satellite-free build (the public cut) this skips — the degrade tests
    above are the ones that matter there."""
    from ws import satellite as ws_sat

    if not ws_sat._SATELLITE_CONFIG_PY.is_file():
        pytest.skip("satellite source tree not in this build")
    version = ws_sat._read_satellite_version_from_source()
    assert version and version[0].isdigit()
    assert ws_sat.satellite_source_available() is True


def test_entry_points_refuse_without_satellite_source(monkeypatch):
    from api.remote import remote_machines as rm
    from ws import satellite as ws_sat

    monkeypatch.setattr(ws_sat, "satellite_source_available", lambda: False)
    with pytest.raises(HTTPException) as exc:
        rm._require_satellite_source()
    assert exc.value.status_code == 404
    assert "not included in this build" in exc.value.detail


def test_entry_points_pass_with_satellite_source(monkeypatch):
    from api.remote import remote_machines as rm
    from ws import satellite as ws_sat

    monkeypatch.setattr(ws_sat, "satellite_source_available", lambda: True)
    rm._require_satellite_source()  # must not raise
