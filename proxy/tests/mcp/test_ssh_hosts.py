"""ssh-hosts — the context-only (transport "none") MCP.

ssh-hosts replaces the community ssh-server wrapper: agents run plain
``ssh``/``scp`` from bash against admin-configured instance hosts. The MCP
contributes NO server process — only instances (authorization + admin UI),
the dynamic-context host list, per-session key materialization, and
network_targets. These tests cover each framework seam.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

from tests.mcp.test_mcp_broker_activation import (  # noqa: E402
    _FakeManifest, _stub_assembly,
)


def _context_only_manifest(name="ssh-hosts"):
    fm = _FakeManifest(name)
    fm.server = SimpleNamespace(
        proxy_callbacks=False, port=0, transport="none", runtime="none",
    )
    return fm


# ---------------------------------------------------------------------------
# Manifest (the real shipped file)
# ---------------------------------------------------------------------------


def test_shipped_manifest_parses():
    from services.mcp.mcp_manifest_parse import _parse_manifest

    path = PROXY_DIR.parent / "mcps" / "custom" / "ssh-hosts" / "manifest.json"
    m = _parse_manifest(path)
    assert m is not None
    assert m.name == "ssh-hosts"
    assert m.server.transport == "none" and m.server.runtime == "none"
    assert m.assignment_mode == "explicit"
    assert m.instances and m.instances.delivery == "none"
    assert {f.key for f in m.instances.fields} == {
        "name", "host", "port", "username", "key_name",
    }
    assert m.data_dirs.get("keys") == "keys/"
    assert m.network_targets and m.network_targets[0].source == "instance"


# ---------------------------------------------------------------------------
# build_session_mcp_config — no server entry; excluded on remote
# ---------------------------------------------------------------------------


def test_context_only_mcp_emits_no_server_entry_locally(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_context_only_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    path, _env, excluded, bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, is_remote=False,
    )

    assert "ssh-hosts" not in excluded  # active — just serverless
    assert "ssh-hosts" not in bundles
    if path:  # config written only when other MCPs produced entries
        import json
        assert "ssh-hosts" not in json.load(open(path)).get("mcpServers", {})


def test_context_only_mcp_excluded_on_remote(monkeypatch, tmp_path):
    from services.mcp import mcp_registry
    _stub_assembly(
        monkeypatch, [_context_only_manifest()], env_by_mcp={}, tmp_path=tmp_path,
    )

    _path, _env, excluded, _bundles, _bash = mcp_registry.build_session_mcp_config(
        "agent", None, is_remote=True,
    )

    # Default (no target_admin_paired) fails closed — user-paired and unknown
    # targets never get key material; the admin-paired allow case lives in
    # test_session_file_broker.py.
    assert "ssh-hosts" in excluded
    assert "admin-paired" in excluded["ssh-hosts"]


# ---------------------------------------------------------------------------
# Dynamic-context provider
# ---------------------------------------------------------------------------


def _instances(*rows):
    return [
        {"id": i, "field_values": dict(fv), "agents": ["agent"],
         "assigned_to_all": False}
        for i, fv in enumerate(rows, start=1)
    ]


def test_provider_renders_authorized_hosts():
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = _instances(
        {"name": "prod", "host": "10.0.0.5", "port": "2222",
         "username": "root", "key_name": "prod_key"},
        {"name": "", "host": "backup.lan", "username": "oto"},
    )
    with patch("storage.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        text = _ssh_hosts_context("agent")

    assert "## SSH Hosts" in text
    # accept-new on every line: the first connect in a non-interactive shell
    # must not die on ssh's TOFU check (hosts are often reachable only from
    # the machine the session runs on — no platform-side pre-scan).
    # ControlMaster mux on local sessions (bwrap = Linux): command bursts
    # reuse one authenticated connection instead of scan-shaped serial
    # connects (Suricata ET SCAN 2001219, 2026-07-06). The socket lives in
    # the OS runtime dir, NOT $OTO_SSH_KEY_DIR — the key dir's session-secrets
    # nesting overflowed the 108-byte sun_path limit (2026-07-11).
    _MUX = ("-o ControlMaster=auto "
            '-o ControlPath="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/oto-cm-%C" '
            "-o ControlPersist=60s")
    assert ('`ssh -i "$OTO_SSH_KEY_DIR/prod_key" '
            f"-o StrictHostKeyChecking=accept-new {_MUX} "
            "-p 2222 root@10.0.0.5`") in text
    # No key / no port / no name → keyless line, default port, host as label.
    assert ("**backup.lan** — `ssh -o StrictHostKeyChecking=accept-new "
            f"{_MUX} -p 22 oto@backup.lan`") in text


def test_provider_mux_gated_by_target_os():
    """Remote targets: mux only where the satellite reported linux/darwin —
    Windows OpenSSH has no unix-socket ControlMaster, and an unknown OS
    (pre-capability satellite) stays conservative."""
    from services.mcp.dynamic_context import _ssh_hosts_context

    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        linux = _ssh_hosts_context(
            "agent", is_remote=True, target_admin_paired=True, target_os="linux")
        windows = _ssh_hosts_context(
            "agent", is_remote=True, target_admin_paired=True, target_os="windows")
        unknown = _ssh_hosts_context(
            "agent", is_remote=True, target_admin_paired=True)

    assert "ControlMaster=auto" in linux
    assert "ControlMaster" not in windows
    assert "-o StrictHostKeyChecking=accept-new -p 22 u@10.0.0.5" in windows
    assert "ControlMaster" not in unknown


def test_provider_silent_when_remote_or_unauthorized():
    from services.mcp.dynamic_context import _ssh_hosts_context

    with patch("storage.mcp_store.get_mcp_instances_for_agent", return_value=[]):
        assert _ssh_hosts_context("agent") is None
    rows = _instances({"name": "x", "host": "10.0.0.5", "username": "u"})
    with patch("storage.mcp_store.get_mcp_instances_for_agent", return_value=rows):
        assert _ssh_hosts_context("agent", is_remote=True) is None


# ---------------------------------------------------------------------------
# Per-session key materialization
# ---------------------------------------------------------------------------


def _materialize_env(tmp_path, *, assigned=True, instances=None):
    """Patch registry + store around materialize_ssh_keys_for_sandbox."""
    mcp_dir = tmp_path / "ssh-hosts"
    (mcp_dir / "keys").mkdir(parents=True)
    manifest = SimpleNamespace(name="ssh-hosts", mcp_dir=mcp_dir)
    agent_mcps = [manifest] if assigned else []
    return (
        mcp_dir,
        patch("services.mcp.mcp_registry.get_manifest", return_value=manifest),
        patch("services.mcp.mcp_registry.get_agent_mcps", return_value=agent_mcps),
        patch("storage.mcp_store.get_mcp_instances_for_agent",
              return_value=instances or []),
    )


def test_materializer_copies_only_authorized_keys(tmp_path):
    from core.sandbox.session_config_dir import materialize_ssh_keys_for_sandbox

    rows = _instances({"host": "h", "key_name": "good_key"})
    mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, instances=rows)
    (mcp_dir / "keys" / "good_key").write_text("PRIVATE")
    (mcp_dir / "keys" / "other_key").write_text("PRIVATE2")
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()
    # A stale key from a previous session must be wiped.
    (cfg_dir / "ssh").mkdir()
    (cfg_dir / "ssh" / "stale_key").write_text("OLD")

    with p1, p2, p3:
        assert materialize_ssh_keys_for_sandbox("agent", cfg_dir) is True

    dst = cfg_dir / "ssh"
    assert (dst / "good_key").read_text() == "PRIVATE"
    assert not (dst / "other_key").exists()
    assert not (dst / "stale_key").exists()
    assert (dst / "good_key").stat().st_mode & 0o777 == 0o600
    assert dst.stat().st_mode & 0o777 == 0o700


def test_materializer_refuses_traversal_key_names(tmp_path):
    from core.sandbox.session_config_dir import materialize_ssh_keys_for_sandbox

    secret = tmp_path / "outside_secret"
    secret.write_text("LEAK")
    rows = _instances({"host": "h", "key_name": "../../outside_secret"})
    _mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, instances=rows)
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()

    with p1, p2, p3:
        assert materialize_ssh_keys_for_sandbox("agent", cfg_dir) is False
    assert not (cfg_dir / "ssh").exists()


def test_materializer_noop_for_unassigned_agent(tmp_path):
    from core.sandbox.session_config_dir import materialize_ssh_keys_for_sandbox

    rows = _instances({"host": "h", "key_name": "k"})
    mcp_dir, p1, p2, p3 = _materialize_env(tmp_path, assigned=False, instances=rows)
    (mcp_dir / "keys" / "k").write_text("PRIVATE")
    cfg_dir = tmp_path / ".claude"
    cfg_dir.mkdir()

    with p1, p2, p3:
        assert materialize_ssh_keys_for_sandbox("agent", cfg_dir) is False
    assert not (cfg_dir / "ssh").exists()


# ---------------------------------------------------------------------------
# Satellite sync skips context-only MCPs
# ---------------------------------------------------------------------------


def test_mcp_sync_diff_skips_runtime_none():
    from services.mcp import mcp_sync

    manifest = SimpleNamespace(
        server=SimpleNamespace(runtime="none"), mcp_dir=Path("/nonexistent"),
    )
    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest):
        to_install, to_update, to_remove = mcp_sync._diff(
            desired={"ssh-hosts"}, installed={},
        )
    assert to_install == set() and to_update == set()

