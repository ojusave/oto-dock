"""Tests for the shared MCP installer module (proxy/services/mcp/mcp_installer.py).

Covers the pure pieces — source parsing, system-dep detection, version_hash
stability — without actually running npm/pip. The end-to-end install is
exercised via the existing manual smoke test for the install flow."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services.mcp import mcp_installer


class TestParseSource:
    def test_npm_plain(self):
        p = mcp_installer.parse_source("npm:mcp-mail-server@1.1.13")
        assert p is not None
        assert p.registry == "npm"
        assert p.package == "mcp-mail-server"
        assert p.version == "1.1.13"

    def test_npm_scoped(self):
        p = mcp_installer.parse_source("npm:@playwright/mcp@0.0.55")
        assert p is not None
        assert p.registry == "npm"
        assert p.package == "@playwright/mcp"
        assert p.version == "0.0.55"

    def test_pypi(self):
        p = mcp_installer.parse_source("pypi:ha-mcp@6.6.1")
        assert p is not None
        assert p.registry == "pypi"
        assert p.package == "ha-mcp"
        assert p.version == "6.6.1"

    def test_docker(self):
        p = mcp_installer.parse_source("docker:collabora")
        assert p is not None
        assert p.registry == "docker"

    def test_empty(self):
        assert mcp_installer.parse_source("") is None

    def test_unknown_prefix(self):
        assert mcp_installer.parse_source("cargo:foo@1.0") is None

    # ---- Unpinned sources: community node/python MCPs carry no version in the
    # catalog (the upstream registry is the version of record). parse_source
    # returns version="" rather than None so install + detection still work.

    def test_npm_unpinned(self):
        p = mcp_installer.parse_source("npm:mcp-mail-server")
        assert p is not None
        assert (p.registry, p.package, p.version) == ("npm", "mcp-mail-server", "")

    def test_npm_scoped_unpinned(self):
        p = mcp_installer.parse_source("npm:@notionhq/notion-mcp-server")
        assert p is not None
        assert (p.registry, p.package, p.version) == ("npm", "@notionhq/notion-mcp-server", "")

    def test_pypi_unpinned(self):
        p = mcp_installer.parse_source("pypi:workspace-mcp")
        assert p is not None
        assert (p.registry, p.package, p.version) == ("pypi", "workspace-mcp", "")

    def test_empty_package_is_none(self):
        # A prefix with no package is not a valid source.
        assert mcp_installer.parse_source("npm:") is None
        assert mcp_installer.parse_source("pypi:") is None


class TestVersionHash:
    def test_stable(self, tmp_path: Path):
        """Same inputs → same hash."""
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        (tmp_path / "requirements.txt").write_text("foo==1.0\n")
        h1 = mcp_installer.compute_version_hash(tmp_path)
        h2 = mcp_installer.compute_version_hash(tmp_path)
        assert h1 == h2
        assert len(h1) == 16  # first 16 hex chars

    def test_changes_when_manifest_changes(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "manifest.json").write_text('{"name": "x","v":"1"}')
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_includes_patches(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "patches").mkdir()
        (tmp_path / "patches" / "foo.patch").write_text("--- a\n+++ b\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_missing_files_ok(self, tmp_path: Path):
        """No install-relevant files → empty hash of nothing (still stable)."""
        h = mcp_installer.compute_version_hash(tmp_path)
        assert len(h) == 16

    # ---- Source-file hashing (added when the manifest-only hash was
    # missing edits to server.py and causing satellite drift).

    def test_changes_when_top_level_source_changes(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        (tmp_path / "server.py").write_text("print('v1')\n")
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "server.py").write_text("print('v2')\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_changes_when_nested_source_changes(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "helper.py").write_text("x = 1\n")
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "lib" / "helper.py").write_text("x = 2\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before != after

    def test_picks_up_all_source_extensions(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        baseline = mcp_installer.compute_version_hash(tmp_path)
        for ext in (".py", ".js", ".mjs", ".ts", ".tsx", ".go", ".rs"):
            f = tmp_path / f"add{ext}"
            f.write_text(f"// content for {ext}\n")
            after = mcp_installer.compute_version_hash(tmp_path)
            assert after != baseline, f"hash didn't change after adding {ext}"
            f.unlink()  # reset

    def test_excludes_pycache(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "compiled.cpython-310.pyc").write_text("bytecode")
        (cache_dir / "noise.py").write_text("noise = 1\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_node_modules(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.js").write_text("module.exports = 1\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_venv(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        (tmp_path / "venv" / "lib").mkdir(parents=True)
        (tmp_path / "venv" / "lib" / "site.py").write_text("# venv noise\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_dotgit_screenshots_backups(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        before = mcp_installer.compute_version_hash(tmp_path)
        for d in (".git", "screenshots", ".backups", "dist", "build"):
            sub = tmp_path / d
            sub.mkdir()
            (sub / "noise.py").write_text(f"# {d}\n")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_excludes_compiled_artifacts(self, tmp_path: Path):
        (tmp_path / "manifest.json").write_text('{}')
        (tmp_path / "server.py").write_text("# real source\n")
        before = mcp_installer.compute_version_hash(tmp_path)
        # Compiled artifacts in the same dir as legitimate source.
        (tmp_path / "server.pyc").write_bytes(b"\x00bytecode")
        (tmp_path / "native.so").write_bytes(b"\x7fELF noise")
        (tmp_path / "ext.dylib").write_bytes(b"\xfe\xed\xfa\xce")
        after = mcp_installer.compute_version_hash(tmp_path)
        assert before == after

    def test_deterministic_irrespective_of_creation_order(self, tmp_path: Path):
        """Creating files in different orders must produce the same hash."""
        (tmp_path / "manifest.json").write_text('{}')
        (tmp_path / "a.py").write_text("a\n")
        (tmp_path / "b.py").write_text("b\n")
        (tmp_path / "c.py").write_text("c\n")
        h1 = mcp_installer.compute_version_hash(tmp_path)
        for f in ("a.py", "b.py", "c.py"):
            (tmp_path / f).unlink()
        # Recreate in reverse order; mtimes differ but the hash is
        # content + path based and should match.
        (tmp_path / "c.py").write_text("c\n")
        (tmp_path / "b.py").write_text("b\n")
        (tmp_path / "a.py").write_text("a\n")
        h2 = mcp_installer.compute_version_hash(tmp_path)
        assert h1 == h2

    def test_skips_double_hash_of_manifest_when_nested_name_collides(self, tmp_path: Path):
        """A nested file named ``manifest.json`` (rare but possible) must not
        accidentally double-hash through the source-walk path."""
        (tmp_path / "manifest.json").write_text('{"v": 1}')
        h_baseline = mcp_installer.compute_version_hash(tmp_path)
        sub = tmp_path / "fixtures"
        sub.mkdir()
        (sub / "manifest.json").write_text('{"fixture": true}')
        h_with_nested = mcp_installer.compute_version_hash(tmp_path)
        # Nested manifest.json is filtered by name from the walker and isn't
        # in _HASH_INPUT_FILES' top-level dir, so it's silently ignored —
        # baseline hash is unchanged. (If a future MCP needs to ship a
        # nested manifest.json as a runtime fixture, we'd revisit this.)
        assert h_baseline == h_with_nested


class TestNodePackageJson:
    """The single canonical serializer used for both the pre-install write and
    the post-readback re-canonicalize — must be byte-identical so the proxy
    (resolved "latest") and a satellite (pinned source) land on the same hash."""

    def test_byte_identical_for_same_inputs(self):
        a = mcp_installer._node_package_json("@scope/x", "1.2.3")
        b = mcp_installer._node_package_json("@scope/x", "1.2.3")
        assert a == b

    def test_is_valid_json_with_expected_shape(self):
        raw = mcp_installer._node_package_json("mcp-mail-server", "latest")
        data = json.loads(raw)
        assert data == {"private": True, "dependencies": {"mcp-mail-server": "latest"}}

    def test_returns_lf_bytes_for_cross_os_hash_stability(self):
        """Must be bytes with LF newlines, written via write_bytes. A
        text-mode write turns LF into CRLF on Windows, drifting
        compute_version_hash and looping the MCP into a reinstall on every
        session against a Linux platform."""
        raw = mcp_installer._node_package_json("@scope/x", "1.2.3")
        assert isinstance(raw, bytes)
        assert b"\r" not in raw
        assert b"\n" in raw

    def test_hash_stable_across_recanonicalize(self, tmp_path: Path):
        """Rewriting package.json with the same resolved version must not
        drift compute_version_hash — this is the exact satellite install
        cycle (extract → rewrite → hash → verify next session)."""
        (tmp_path / "manifest.json").write_text('{"name": "x"}')
        pj = tmp_path / "package.json"
        pj.write_bytes(mcp_installer._node_package_json("@scope/x", "1.2.3"))
        before = mcp_installer.compute_version_hash(tmp_path)
        pj.write_bytes(mcp_installer._node_package_json("@scope/x", "1.2.3"))
        assert mcp_installer.compute_version_hash(tmp_path) == before


class TestInstalledVersionReadback:
    def test_node_reads_version(self, tmp_path: Path):
        pkg_dir = tmp_path / "node_modules" / "mcp-mail-server"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.json").write_text('{"name": "mcp-mail-server", "version": "1.2.7"}')
        assert mcp_installer._node_installed_version(tmp_path, "mcp-mail-server") == "1.2.7"

    def test_node_reads_scoped_version(self, tmp_path: Path):
        pkg_dir = tmp_path / "node_modules" / "@notionhq" / "notion-mcp-server"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "package.json").write_text('{"version": "2.4.0"}')
        got = mcp_installer._node_installed_version(tmp_path, "@notionhq/notion-mcp-server")
        assert got == "2.4.0"

    def test_node_missing_returns_empty(self, tmp_path: Path):
        # No node_modules at all → "" (never raises). The node branch treats an
        # empty readback after a successful install as a failure.
        assert mcp_installer._node_installed_version(tmp_path, "nope") == ""

    @pytest.mark.asyncio
    async def test_python_missing_venv_returns_empty(self, tmp_path: Path):
        # No venv interpreter on disk → "" without spawning anything.
        assert await mcp_installer._python_installed_version(tmp_path / "venv", "ha-mcp") == ""


class TestCheckSystemRequirements:
    def test_empty_requirements_returns_empty(self):
        req = mcp_installer.SystemRequirementsInput()
        assert mcp_installer.check_system_requirements(req) == []

    def test_node_min_reported_when_not_installed(self):
        req = mcp_installer.SystemRequirementsInput(node_min="99.0")
        # Either node isn't installed, or it's < 99 — either way the check must flag it.
        with patch("services.mcp.mcp_installer._node_version", return_value=""):
            missing = mcp_installer.check_system_requirements(req)
            assert any(m.kind == "interpreter" and m.name == "node" for m in missing)

    def test_missing_package_reported(self):
        req = mcp_installer.SystemRequirementsInput(debian=["this-package-does-not-exist"])
        with patch("services.mcp.mcp_installer._detect_os_keys", return_value=["debian"]), \
             patch("services.mcp.mcp_installer._is_package_installed", return_value=False):
            missing = mcp_installer.check_system_requirements(req)
            assert len(missing) == 1
            assert missing[0].kind == "package"
            assert missing[0].name == "this-package-does-not-exist"
            assert "apt install" in missing[0].install_cmd

    def test_installed_package_not_reported(self):
        req = mcp_installer.SystemRequirementsInput(debian=["some-package"])
        with patch("services.mcp.mcp_installer._detect_os_keys", return_value=["debian"]), \
             patch("services.mcp.mcp_installer._is_package_installed", return_value=True):
            missing = mcp_installer.check_system_requirements(req)
            assert missing == []


class TestPinLocalManifest:
    """Pinning the LOCAL manifest after an unpinned install — writes both
    `version` and `server.source`, and the pinned source round-trips through
    parse_source (incl. scoped npm)."""

    def _write_unpinned(self, tmp_path: Path, source: str) -> Path:
        mf = tmp_path / "manifest.json"
        mf.write_text(json.dumps({
            "name": "x", "label": "X", "description": "d", "version": "",
            "category": "community", "server": {"runtime": "node", "source": source},
        }, indent=2))
        return mf

    def test_pins_version_and_source_npm_scoped(self, tmp_path: Path):
        from services.mcp import mcp_updater
        self._write_unpinned(tmp_path, "npm:@notionhq/notion-mcp-server")
        mcp_updater.pin_local_manifest(tmp_path, "npm", "@notionhq/notion-mcp-server", "2.4.0")
        data = json.loads((tmp_path / "manifest.json").read_text())
        assert data["version"] == "2.4.0"
        assert data["server"]["source"] == "npm:@notionhq/notion-mcp-server@2.4.0"
        # The pinned source must parse back to the same package + version.
        p = mcp_installer.parse_source(data["server"]["source"])
        assert (p.registry, p.package, p.version) == ("npm", "@notionhq/notion-mcp-server", "2.4.0")

    def test_pins_pypi(self, tmp_path: Path):
        from services.mcp import mcp_updater
        self._write_unpinned(tmp_path, "pypi:workspace-mcp")
        mcp_updater.pin_local_manifest(tmp_path, "pypi", "workspace-mcp", "1.21.3")
        data = json.loads((tmp_path / "manifest.json").read_text())
        assert data["version"] == "1.21.3"
        assert data["server"]["source"] == "pypi:workspace-mcp@1.21.3"


class TestSelfHash:
    def test_returns_hex(self):
        h = mcp_installer.self_hash()
        assert len(h) == 64  # full sha256 hex
        assert all(c in "0123456789abcdef" for c in h)
