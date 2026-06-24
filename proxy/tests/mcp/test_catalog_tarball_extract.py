"""Catalog tarball extraction — folder-name match + manifest-name fallback.

The catalog repo's folder usually shares the MCP's name, but the authoritative
id is the ``name`` inside each folder's manifest.json and the two can differ
(folder ``workspace-mcp`` ships manifest name ``google-workspace``). The
extractor must resolve both, and return None for an unknown name.
"""

import io
import json
import tarfile
from pathlib import Path

from services.community.community_installer import _extract_mcp_subfolder


def _make_tarball(folders: dict[str, dict]) -> bytes:
    """Build a GitHub-style repo tarball: <owner>-<repo>-<sha>/<folder>/..."""
    prefix = "OtoDock-community-mcps-abc1234"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for folder, files in folders.items():
            d = tarfile.TarInfo(name=f"{prefix}/{folder}")
            d.type = tarfile.DIRTYPE
            tf.addfile(d)
            for rel, content in files.items():
                data = content.encode()
                fi = tarfile.TarInfo(name=f"{prefix}/{folder}/{rel}")
                fi.size = len(data)
                tf.addfile(fi, io.BytesIO(data))
    return buf.getvalue()


def _manifest(name: str) -> str:
    return json.dumps({"name": name, "category": "community",
                       "server": {"runtime": "python"}})


def test_extracts_folder_matching_name(tmp_path: Path):
    tb = _make_tarball({
        "camoufox": {"manifest.json": _manifest("camoufox"), "run.py": "x"},
    })
    out = _extract_mcp_subfolder(tb, "camoufox", tmp_path)
    assert out is not None and out.name == "camoufox"
    assert (out / "manifest.json").is_file()
    assert (out / "run.py").read_text() == "x"


def test_falls_back_to_manifest_name_scan(tmp_path: Path):
    tb = _make_tarball({
        "camoufox": {"manifest.json": _manifest("camoufox")},
        "workspace-mcp": {"manifest.json": _manifest("google-workspace"),
                          "server.py": "y"},
    })
    out = _extract_mcp_subfolder(tb, "google-workspace", tmp_path)
    assert out is not None and out.name == "workspace-mcp"
    assert json.loads((out / "manifest.json").read_text())["name"] == "google-workspace"
    assert (out / "server.py").read_text() == "y"


def test_unknown_name_returns_none(tmp_path: Path):
    tb = _make_tarball({
        "camoufox": {"manifest.json": _manifest("camoufox")},
    })
    assert _extract_mcp_subfolder(tb, "no-such-mcp", tmp_path) is None


def test_malformed_manifest_is_skipped(tmp_path: Path):
    tb = _make_tarball({
        "broken": {"manifest.json": "{not json"},
        "workspace-mcp": {"manifest.json": _manifest("google-workspace")},
    })
    out = _extract_mcp_subfolder(tb, "google-workspace", tmp_path)
    assert out is not None and out.name == "workspace-mcp"
