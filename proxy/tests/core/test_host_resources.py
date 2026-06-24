"""Unit tests for container-aware memory detection (core/sandbox/host_resources.py).

``reclaimable_bytes`` is a pure function tested exhaustively; ``live_available_bytes``
and ``detect_memory_limit_bytes`` are tested by monkeypatching the low-level readers
so the cgroup-v2 / v1 / proc ladder + the fail-closed path are pinned regardless of
where the suite runs.
"""

import core.sandbox.host_resources as H

_MIB = 1024 * 1024


# --- detect_memory_limit_bytes (real read) ---------------------------------

def test_detect_memory_positive():
    # A cgroup limit, or the /proc/meminfo host fallback — never <= 0.
    assert H.detect_memory_limit_bytes() > 0


# --- reclaimable_bytes (pure) ----------------------------------------------

def test_reclaimable_v2_subtracts_dirty_and_writeback():
    stat = {"inactive_file": 1000, "slab_reclaimable": 500,
            "file_dirty": 100, "file_writeback": 50, "active_file": 9999}
    # inactive_file + slab_reclaimable - dirty - writeback (active_file ignored)
    assert H.reclaimable_bytes(stat, v2=True) == 1000 + 500 - 100 - 50


def test_reclaimable_v1_has_no_slab_key():
    # cgroup v1 uses total_* hierarchical keys and has NO slab-reclaimable field;
    # a stray slab_reclaimable must be ignored on the v1 path.
    stat = {"total_inactive_file": 1000, "total_dirty": 100,
            "total_writeback": 50, "slab_reclaimable": 99999}
    assert H.reclaimable_bytes(stat, v2=False) == 1000 - 100 - 50


def test_reclaimable_clamps_to_zero():
    stat = {"inactive_file": 100, "file_dirty": 999, "file_writeback": 999}
    assert H.reclaimable_bytes(stat, v2=True) == 0


def test_reclaimable_missing_keys_are_zero():
    assert H.reclaimable_bytes({}, v2=True) == 0
    assert H.reclaimable_bytes({}, v2=False) == 0


# --- live_available_bytes (monkeypatched ladder) ---------------------------

def test_live_available_capped_v2(monkeypatch):
    monkeypatch.setattr(H, "_capped_limit", lambda: (4000 * _MIB, "v2"))
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)        # memory.current
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {"inactive_file": 300 * _MIB})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 64_000 * _MIB)  # host roomy
    # (4000 - 2500) + 300 reclaimable = 1800 MiB (< host ⇒ cgroup term wins)
    assert H.live_available_bytes() == 1800 * _MIB


def test_live_available_capped_missing_stat(monkeypatch):
    monkeypatch.setattr(H, "_capped_limit", lambda: (4000 * _MIB, "v2"))
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {})         # no reclaimable credit
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 64_000 * _MIB)
    assert H.live_available_bytes() == 1500 * _MIB


def test_live_available_capped_takes_host_min(monkeypatch):
    # A leaky SIBLING container eats host RAM without touching this cgroup:
    # cgroup headroom says 1800 MiB, the host has only 400 left. Gate 2 must
    # see 400 — a cgroup-only read admits straight into the host OOM killer
    # (the 2026-07-06 camoufox incident, with the proxy capped).
    monkeypatch.setattr(H, "_capped_limit", lambda: (4000 * _MIB, "v2"))
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {"inactive_file": 300 * _MIB})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 400 * _MIB)
    assert H.live_available_bytes() == 400 * _MIB


def test_live_available_capped_host_unreadable_falls_back_to_cgroup(monkeypatch):
    monkeypatch.setattr(H, "_capped_limit", lambda: (4000 * _MIB, "v2"))
    monkeypatch.setattr(H, "_read_int", lambda path: 2500 * _MIB)
    monkeypatch.setattr(H, "_read_memory_stat", lambda path: {})
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: None)
    assert H.live_available_bytes() == 1500 * _MIB


def test_live_available_uncapped_uses_meminfo(monkeypatch):
    monkeypatch.setattr(H, "_capped_limit", lambda: None)
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: 7000 * _MIB)
    assert H.live_available_bytes() == 7000 * _MIB


def test_live_available_fail_closed(monkeypatch):
    # Nothing readable → 0 (⇒ GATE 2 admits nothing), never a guess.
    monkeypatch.setattr(H, "_capped_limit", lambda: None)
    monkeypatch.setattr(H, "_proc_meminfo_available_bytes", lambda: None)
    assert H.live_available_bytes() == 0


def test_live_available_real_read_nonnegative():
    assert H.live_available_bytes() >= 0
