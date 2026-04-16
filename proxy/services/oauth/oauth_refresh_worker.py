"""OAuth token refresh worker — background asyncio task that proactively
refreshes tokens whose access lifetime is about to expire.

Without this, an agent that hasn't run for an hour pays a refresh
round-trip on its next tool call (lazy refresh inside ``provider.refresh``).
The worker keeps every connected account's token fresh in the background
so the user-perceived first-call latency stays low.

Lifecycle:
  * Created from ``proxy/app.py`` lifespan AFTER
    ``mcp_registry.scan_manifests()``.
  * Sleeps for ``_INTERVAL_SECONDS`` (default 60s).
  * Cancelled BEFORE ``_shutdown_sessions`` so it doesn't fight writeback
    for per-account locks during proxy shutdown.

Per-account safety:
  * Holds ``credential_locks.get_lock(user_sub, provider_id, account_label)``
    while reading + refreshing + writing back. Lazy refresh on the request
    path uses the same lock — they serialize, never interleave.
  * Refresh always re-persists BOTH access AND refresh tokens — some
    vendors rotate the refresh token on every refresh; preserving the
    previous one only when the response omits it (handled by each
    provider's ``refresh()``) avoids silent token loss.

Lock scope: the lock key is **provider-scoped**, not
MCP-scoped. Multiple MCPs of the same provider share the OAuth grant +
token file + lock, so concurrent refresh attempts across MCPs of the
same provider serialize correctly.

Discovery: walks ``sessions/*-tokens/`` for any provider's
token files. The directory name IS the provider_id (e.g.,
``google-tokens`` → ``google``). Token files in the new
``generic_oauth_v1`` shape carry a top-level ``provider`` field for
defensive cross-check; legacy ``workspace_mcp`` files fall back to the
dir-name convention.

Failure mode:
  * Network error / vendor 5xx → log + skip this account, try again next tick.
  * 4xx (invalid_grant — token was revoked at the vendor) → log + leave the
    file in place; next session start will exclude the MCP with a clear
    "not connected" reason.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger("claude-proxy.oauth-refresh-worker")

# How often the loop wakes up.
_INTERVAL_SECONDS = 60
# Refresh threshold: tokens with less than this lifetime get refreshed.
_REFRESH_THRESHOLD_SECONDS = 300

# Module-level handle to the running task so app.py can cancel it cleanly.
_worker_task: asyncio.Task | None = None


def start_worker() -> asyncio.Task:
    """Spawn the background refresh task. Idempotent."""
    global _worker_task
    if _worker_task and not _worker_task.done():
        return _worker_task
    _worker_task = asyncio.create_task(_refresh_loop(), name="oauth-refresh-worker")
    logger.info("OAuth refresh worker started (interval=%ds, threshold=%ds)",
                _INTERVAL_SECONDS, _REFRESH_THRESHOLD_SECONDS)
    return _worker_task


async def stop_worker() -> None:
    """Cancel + await the worker. Idempotent; safe during shutdown."""
    global _worker_task
    if not _worker_task:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except (asyncio.CancelledError, Exception):
        pass
    _worker_task = None
    logger.info("OAuth refresh worker stopped")


async def _refresh_loop() -> None:
    """Main loop: scan every token file, refresh those near expiry."""
    while True:
        try:
            await asyncio.sleep(_INTERVAL_SECONDS)
            await _refresh_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OAuth refresh tick failed (continuing)")


async def _refresh_tick() -> None:
    """One pass: scan every provider's token dir and refresh near-expiry tokens.

    Walks ``sessions/*-tokens/{username}/{account_label}.json``.
    The dir name (``google-tokens``, ``slack-tokens``, …) gives the
    provider_id. Each leaf file is one (provider, user, account) tuple.
    """
    base = config.SESSIONS_DIR
    if not base.is_dir():
        return

    refreshed = 0
    for prov_dir in base.glob("*-tokens"):
        if not prov_dir.is_dir():
            continue
        provider_id = prov_dir.name.removesuffix("-tokens")
        for user_dir in prov_dir.iterdir():
            if not user_dir.is_dir():
                continue
            for token_file in user_dir.glob("*.json"):
                try:
                    if await _maybe_refresh_token_file(
                        token_file=token_file,
                        username=user_dir.name,
                        provider_id=provider_id,
                    ):
                        refreshed += 1
                except Exception:
                    logger.exception(
                        "OAuth refresh failed for %s/%s",
                        prov_dir.name, token_file.name,
                    )
    if refreshed:
        logger.info("OAuth refresh worker refreshed %d token(s)", refreshed)


async def _maybe_refresh_token_file(
    *, token_file: Path, username: str, provider_id: str,
) -> bool:
    """Refresh the token in ``token_file`` IF it's near expiry. Returns True
    if a refresh was performed.

    All token files are ``generic_oauth_v1`` shape; manifest-declared
    aliases get re-emitted on write-back so legacy-shape MCP readers
    (workspace-mcp's ``google.auth``) keep working.
    """
    from services.billing import relay_client
    from services.mcp import mcp_registry
    from services.oauth import oauth_account_store
    from auth.oauth_providers import get_provider
    from core.credentials import credential_locks

    raw = oauth_account_store._read_oauth_token(token_file)
    if raw is None:
        return False

    expiry_str = raw.get("expires_at") or ""
    if not expiry_str:
        return False
    try:
        expiry_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
        if expiry_dt.tzinfo is None:
            expiry_dt = expiry_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return False

    remaining = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
    if remaining > _REFRESH_THRESHOLD_SECONDS:
        return False

    # Four-arm dispatch:
    #   * HOSTED (extra.via_relay) → relay refreshes server-side with OtoDock's
    #     secret. No local client_secret exists. Checked FIRST.
    #   * PAT (extra.flow == "personal_access_token") → never refresh (zero-expiry).
    #   * S2S (extra.flow == "client_credentials") → re-exchange via
    #     provider.exchange_client_credentials. Zoom S2S tokens last
    #     1 hour and have NO refresh_token — they must be re-minted.
    #   * Standard OAuth → provider.refresh with the stored refresh_token.
    file_extra = raw.get("extra", {}) or {}
    flow_marker = file_extra.get("flow", "")
    is_relay = bool(file_extra.get("via_relay"))

    if is_relay and not relay_client.is_available():
        # Hosted token but the relay isn't reachable yet — can't refresh
        # without OtoDock's secret. Skip quietly; retry once the relay is live.
        return False

    if flow_marker == "personal_access_token":
        return False

    is_s2s = flow_marker == "client_credentials"
    refresh_token = raw.get("refresh_token") or ""
    if not is_s2s and not refresh_token:
        # Standard / hosted OAuth: nothing to refresh with (vendor never
        # returned a refresh_token, or it was lost). Skip — lazy refresh on
        # first use will surface the error to the user.
        return False

    # Hosted tokens deliberately persist NO client_secret (the relay holds it),
    # so the local-credential guard applies only to self-managed tokens.
    client_id = raw.get("client_id", "")
    client_secret = raw.get("client_secret", "")
    if not is_relay and (not client_id or not client_secret):
        return False

    account_label = token_file.stem  # "{account_label}.json"
    user_sub = ""
    if username:
        # Reverse-resolve username → user_sub via DB.
        with __import__("storage.pg", fromlist=["get_conn"]).get_conn() as conn:
            row = conn.execute(
                "SELECT sub FROM users WHERE username = %s", (username,),
            ).fetchone()
            user_sub = row["sub"] if row else ""

    # Lock key: provider-scoped (multiple MCPs sharing this provider
    # share the OAuth grant + token file).
    lock_key = (user_sub or "_service", provider_id, account_label)

    async with credential_locks.get_lock(*lock_key):
        # Re-read inside the lock — a concurrent lazy refresh may have
        # rotated the tokens since we made the decision to refresh.
        raw2 = oauth_account_store._read_oauth_token(token_file)
        if raw2 is None:
            return False
        file_extra2 = raw2.get("extra", {}) or {}
        is_relay2 = bool(file_extra2.get("via_relay"))
        is_s2s2 = file_extra2.get("flow") == "client_credentials"

        provider = get_provider(provider_id)
        if is_relay2:
            # HOSTED: the relay refreshes with OtoDock's secret and returns the
            # user's new tokens (its TokenSet.raw keeps {"via_relay": True}, so
            # the marker survives the writeback below).
            new_ts = await relay_client.oauth_refresh(
                provider_id=provider_id,
                refresh_token=raw2.get("refresh_token") or refresh_token,
            )
            # Re-run the provider's normalizer over the vendor's verbatim
            # response (mirrors do_oauth_exchange) so provider-specific
            # flattening lands in extra. The relay envelope keeps the old
            # refresh_token when the vendor omits one — raw doesn't, so
            # carry it across the rebuild.
            if new_ts.raw:
                _kept_refresh = new_ts.refresh_token
                new_ts = provider.normalize_token_response(new_ts.raw)
                if not new_ts.refresh_token:
                    new_ts.refresh_token = _kept_refresh
        elif is_s2s2:
            # S2S re-exchange. Pass account_id from file extra (vendor
            # response doesn't echo it — caller persisted it at first
            # exchange; we pass it back so the re-exchange call targets
            # the same Zoom account).
            new_ts = await provider.exchange_client_credentials(
                client_id=client_id,
                client_secret=client_secret,
                scopes=[],
                extra={"account_id": file_extra2.get("account_id", "")} or None,
            )
        else:
            refresh_token2 = raw2.get("refresh_token") or refresh_token
            new_ts = await provider.refresh(
                refresh_token=refresh_token2,
                client_id=client_id,
                client_secret=client_secret,
            )

        token_url = raw2.get("token_url") or provider.token_url

        # Aliases come from any MCP using this provider — all MCPs
        # sharing a provider share the alias declaration (provider-level
        # concern, not per-MCP).
        manifests = mcp_registry.get_mcps_by_provider(provider_id)
        aliases = None
        if manifests:
            tf = (manifests[0].credentials.oauth or {}).get("token_format", {}) or {}
            aliases = tf.get("aliases") or None
        # Preserve previously-captured vendor metadata (team_id,
        # tenant_id, account_id, flow, preferred_bearer, …). Merge in
        # any new fields from the refresh / re-exchange response.
        extra = dict(file_extra2)
        if new_ts.raw:
            for k, v in new_ts.raw.items():
                if k not in (
                    "access_token", "refresh_token", "expires_in",
                    "scope", "token_type",
                ):
                    extra[k] = v
        # For S2S, the re-exchange response doesn't carry `flow` or
        # `account_id` — guarantee they survive by re-asserting.
        if is_s2s2:
            extra["flow"] = "client_credentials"
            if file_extra2.get("account_id"):
                extra["account_id"] = file_extra2["account_id"]

        oauth_account_store._write_generic_oauth_v1_token(
            token_file,
            provider_id=provider_id,
            account_id=raw2.get("account_id", ""),
            access_token=new_ts.access_token,
            refresh_token=new_ts.refresh_token,
            expires_in=new_ts.expires_in,
            scopes=raw2.get("scopes", []),
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            extra=extra,
            aliases=aliases,
        )

        logger.debug(
            "Refreshed token: provider=%s user=%s account=%s flow=%s remaining_before=%.0fs",
            provider_id, (user_sub or "_service")[:8], account_label,
            "s2s" if is_s2s2 else "oauth", remaining,
        )
        return True
