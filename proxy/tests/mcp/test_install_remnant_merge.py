"""Fresh install onto an existing unregistered folder must merge, not crash.

A community MCP's folder can exist on disk without a registry entry — e.g.
ssh-server's preserved ``keys/`` data dir carried across a migration, or a
half-cleaned install. The fresh-install branch used a plain copytree, which
raised FileExistsError; it must merge instead, keeping preserved data dirs.
"""

from pathlib import Path

from services.community.community_installer import _apply_extracted_files


def _mk(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root


def test_fresh_install_merges_into_existing_remnant(tmp_path: Path):
    src = _mk(tmp_path / "src", {"manifest.json": "{}", "server.py": "new"})
    target = _mk(tmp_path / "community" / "ssh-server", {
        "keys/id_ed25519": "SECRET",       # preserved data dir remnant
        "server.py": "old",
    })

    _apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert (target / "manifest.json").is_file()
    assert (target / "server.py").read_text() == "new"          # incoming wins
    assert (target / "keys" / "id_ed25519").read_text() == "SECRET"  # kept


def test_fresh_install_plain_copy_when_target_absent(tmp_path: Path):
    src = _mk(tmp_path / "src", {"manifest.json": "{}"})
    target = tmp_path / "community" / "new-mcp"

    _apply_extracted_files(src, target, is_update=False, backup_dir=None)

    assert (target / "manifest.json").is_file()
