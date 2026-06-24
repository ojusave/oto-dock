"""credential_locks key is provider-scoped, not MCP-scoped.

Multiple MCPs of the same provider share the OAuth token file + grant.
The lock must therefore be keyed on (user_sub, provider_id, account_label)
so concurrent operations on the shared file serialize correctly.

Today there's exactly one MCP per provider, so the change is exercised
indirectly by the refresh worker + writeback tests. These tests verify
the explicit contract.
"""

from __future__ import annotations

import asyncio

import pytest

from core.credentials import credential_locks


def test_get_lock_returns_same_lock_per_provider_key():
    """Same (user, provider, account) returns the same Lock object."""
    l1 = credential_locks.get_lock("alice", "google", "work")
    l2 = credential_locks.get_lock("alice", "google", "work")
    assert l1 is l2


def test_two_mcps_same_provider_can_use_same_lock_key():
    """Two MCPs sharing provider_id="google" both call get_lock with
    "google" as the 2nd arg — they MUST get the same lock so they don't
    race on the shared token file."""
    # MCP A (e.g. google-workspace) and MCP B (e.g. hypothetical
    # google-bigquery) both call with provider_id="google".
    lock_for_mcp_a = credential_locks.get_lock("alice", "google", "work")
    lock_for_mcp_b = credential_locks.get_lock("alice", "google", "work")
    assert lock_for_mcp_a is lock_for_mcp_b


def test_different_providers_get_distinct_locks():
    """User has Slack AND Linear connected — they're independent."""
    google_lock = credential_locks.get_lock("alice", "google", "work")
    slack_lock = credential_locks.get_lock("alice", "slack", "work")
    linear_lock = credential_locks.get_lock("alice", "linear", "work")
    assert google_lock is not slack_lock
    assert google_lock is not linear_lock
    assert slack_lock is not linear_lock


@pytest.mark.asyncio
async def test_provider_scoped_lock_serializes_concurrent_callers():
    """Two coroutines for the same (user, provider, account) serialize."""
    order = []
    lock = credential_locks.get_lock("alice", "linear", "work")

    async def section(label: str):
        async with lock:
            order.append(f"{label}-start")
            await asyncio.sleep(0.02)
            order.append(f"{label}-end")

    await asyncio.gather(section("A"), section("B"))

    # Must be A-start, A-end, B-start, B-end (or B-...-A-...) — never interleaved.
    assert order in (
        ["A-start", "A-end", "B-start", "B-end"],
        ["B-start", "B-end", "A-start", "A-end"],
    )
