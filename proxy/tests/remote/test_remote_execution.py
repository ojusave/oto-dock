"""Regression tests for core/remote/remote_execution.py."""

from __future__ import annotations


def test_load_hook_scripts_loads_all_hooks():
    """Regression: after the core/ -> core/remote/ subpackage move,
    ``_load_hook_scripts`` computed ``hooks_dir`` from a stale ``__file__``
    parent-count (and its ``app_config.PROXY_DIR`` primary branch never fired —
    config only exposes ``HOOKS_DIR``), so it resolved to ``proxy/core/hooks``
    (absent) and returned an EMPTY dict. Remote/satellite sessions therefore ran
    with NO permission_gate / tool_result_forwarder / subagent_tracker hooks.

    Pin that all three hook scripts load from the real proxy/hooks dir with
    non-empty content.
    """
    from core.remote import remote_execution as re

    re._HOOK_SCRIPTS_CACHE = None  # bypass the module-level cache
    try:
        scripts = re._load_hook_scripts()
    finally:
        re._HOOK_SCRIPTS_CACHE = None  # don't leak forced state to other tests

    for name in ("permission_gate.py", "tool_result_forwarder.py",
                 "subagent_tracker.py"):
        assert name in scripts, f"hook script not loaded: {name}"
        assert scripts[name].strip(), f"hook script empty: {name}"
