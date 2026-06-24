"""Tests for MCP tarball cache (services/mcp/mcp_tarball.py)."""

from pathlib import Path
from unittest.mock import MagicMock, patch


def _fake_manifest(mcp_dir: Path, name: str = "fake-mcp"):
    m = MagicMock()
    m.mcp_dir = mcp_dir
    m.name = name
    m.server_name = name
    m.category = "custom"
    m.version = "1.0.0"
    return m


def test_tarball_excludes_heavy_dirs(tmp_path: Path, temp_db, monkeypatch):
    """venv/, node_modules/, __pycache__/, .git/ must not end up in the tarball."""
    import tarfile, io, base64
    import config as app_config
    from services.mcp import mcp_tarball

    mcp_dir = tmp_path / "my-mcp"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text('{"name":"my-mcp"}')
    (mcp_dir / "requirements.txt").write_text("foo==1.0\n")
    (mcp_dir / "server.py").write_text("print('hi')\n")
    (mcp_dir / "venv").mkdir()
    (mcp_dir / "venv" / "big.so").write_bytes(b"x" * 1000)
    (mcp_dir / "node_modules").mkdir()
    (mcp_dir / "node_modules" / "foo.js").write_text("m")
    (mcp_dir / "__pycache__").mkdir()
    (mcp_dir / "__pycache__" / "x.pyc").write_bytes(b"x")

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")

    with patch("services.mcp.mcp_registry.get_manifest",
               return_value=_fake_manifest(mcp_dir, "my-mcp")):
        r = mcp_tarball.build_tarball("my-mcp")

    # Decode + inspect
    data = base64.b64decode(r.tarball_b64)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "requirements.txt" in names
    assert "server.py" in names
    assert not any(n.startswith("venv/") for n in names)
    assert not any(n.startswith("node_modules/") for n in names)
    assert not any(n.startswith("__pycache__/") for n in names)


def test_tarball_excludes_data_dirs_and_keys(tmp_path: Path, temp_db, monkeypatch):
    """Manifest-declared data_dirs (ssh-server's keys/ + config/) never ship.

    The keys dir holds raw private keys; the tarball goes to every satellite
    that syncs the MCP — including user-paired laptops — so shipping it was a
    key-material leak (masked only by cache staleness: key files don't drift
    compute_version_hash)."""
    import tarfile, io, base64
    import config as app_config
    from services.mcp import mcp_tarball

    mcp_dir = tmp_path / "ssh-server"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text('{"name":"ssh-server"}')
    (mcp_dir / "package.json").write_text('{"name":"x"}')
    (mcp_dir / "keys").mkdir()
    (mcp_dir / "keys" / "id_ed25519").write_text("PRIVATE KEY MATERIAL")
    (mcp_dir / "config").mkdir()
    (mcp_dir / "config" / "hosts.json").write_text('{"hosts": []}')

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")

    manifest = _fake_manifest(mcp_dir, "ssh-server")
    manifest.data_dirs = {"config": "config/", "keys": "keys/"}
    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest):
        r = mcp_tarball.build_tarball("ssh-server")

    data = base64.b64decode(r.tarball_b64)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert "manifest.json" in names
    assert "package.json" in names
    assert not any(n.startswith("keys") for n in names)
    assert not any(n.startswith("config") for n in names)


def test_tarball_keys_floor_without_declaration(tmp_path: Path, temp_db, monkeypatch):
    """keys/ is excluded even when the manifest declares NO data_dirs
    (defense in depth); other dirs like config/ ship normally then."""
    import tarfile, io, base64
    import config as app_config
    from services.mcp import mcp_tarball

    mcp_dir = tmp_path / "some-mcp"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text('{"name":"some-mcp"}')
    (mcp_dir / "keys").mkdir()
    (mcp_dir / "keys" / "secret").write_text("SECRET")
    (mcp_dir / "config").mkdir()
    (mcp_dir / "config" / "defaults.json").write_text("{}")

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")

    manifest = _fake_manifest(mcp_dir, "some-mcp")
    manifest.data_dirs = {}
    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest):
        r = mcp_tarball.build_tarball("some-mcp")

    data = base64.b64decode(r.tarball_b64)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
    assert not any(n.startswith("keys") for n in names)
    assert "config/defaults.json" in names


def test_invalidate_drops_legacy_format_entries(tmp_path: Path, temp_db, monkeypatch):
    """invalidate() drops pre-format cache files (they may contain data_dirs
    from before the exclusion fix and are never served again) and keeps only
    current-format entries whose hash matches a registered MCP."""
    import config as app_config
    from services.mcp import mcp_tarball, mcp_installer

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")

    mcp_dir = tmp_path / "m"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text('{"name":"m"}')
    valid_hash = mcp_installer.compute_version_hash(mcp_dir)

    d = mcp_tarball._cache_dir()
    legacy = d / f"{valid_hash}.tar.gz"                              # pre-format
    current_valid = d / f"{valid_hash}.{mcp_tarball._TARBALL_FORMAT}.tar.gz"
    current_stale = d / f"deadbeefdeadbeef.{mcp_tarball._TARBALL_FORMAT}.tar.gz"
    for f in (legacy, current_valid, current_stale):
        f.write_bytes(b"x")

    manifest = _fake_manifest(mcp_dir, "m")
    with patch("services.mcp.mcp_registry.get_all_manifests",
               return_value={"m": manifest}):
        mcp_tarball.invalidate("m")

    assert not legacy.exists()
    assert current_valid.exists()
    assert not current_stale.exists()


def test_tarball_reuses_cache(tmp_path: Path, temp_db, monkeypatch):
    """Second build with same version_hash reuses the cached file."""
    import config as app_config
    from services.mcp import mcp_tarball

    mcp_dir = tmp_path / "m"
    mcp_dir.mkdir()
    (mcp_dir / "manifest.json").write_text('{"name":"m"}')
    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")

    with patch("services.mcp.mcp_registry.get_manifest",
               return_value=_fake_manifest(mcp_dir, "m")):
        r1 = mcp_tarball.build_tarball("m")
        r2 = mcp_tarball.build_tarball("m")
    # Same version_hash → cached entry reused.
    assert r1.version_hash == r2.version_hash
    cache = mcp_tarball._cache_path_for(r1.version_hash)
    assert cache.is_file()


def test_gc_removes_stale(tmp_path: Path, temp_db, monkeypatch):
    """gc() evicts entries whose mtime is older than STALE_CACHE_AGE_S."""
    import os
    import config as app_config
    from services.mcp import mcp_tarball

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")
    d = mcp_tarball._cache_dir()

    stale = d / "old.tar.gz"
    stale.write_bytes(b"x" * 100)
    # Set mtime far in the past
    past = 0.0
    os.utime(stale, (past, past))

    fresh = d / "new.tar.gz"
    fresh.write_bytes(b"y" * 100)

    freed = mcp_tarball.gc()
    assert freed == 100
    assert not stale.exists()
    assert fresh.exists()


def test_missing_mcp_raises(tmp_path: Path, temp_db, monkeypatch):
    import config as app_config
    from services.mcp import mcp_tarball

    monkeypatch.setattr(app_config, "BASE_DIR", tmp_path / "proxy")
    with patch("services.mcp.mcp_registry.get_manifest", return_value=None):
        try:
            mcp_tarball.build_tarball("ghost")
            assert False, "expected FileNotFoundError"
        except FileNotFoundError:
            pass
