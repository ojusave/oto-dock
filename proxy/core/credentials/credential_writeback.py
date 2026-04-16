"""Credential directory writeback — shared across execution layers.

Copies refreshed OAuth token files from the per-session ``.credentials``
dirs back to the central credential store on session close. This ensures
token refreshes performed by MCPs during a LOCAL session are preserved for
future sessions.

Remote sessions are out of scope by design: satellites receive transient
per-session token copies over the session-file broker channel and
``.credentials`` never syncs back (push-only + not manifested), so a
satellite-side MCP refresh dies with the session — the platform's
background refresh worker is the authoritative refresher either way.

Async + per-account locking via ``core.credentials.credential_locks``: all three of
(lazy refresh in `provider.refresh`, background refresh worker, end-of-
session writeback) coordinate via the same
per-(user_sub, mcp_name, account_label) lock so refreshes never
interleave with partial writebacks.
"""

import asyncio
import logging

import config as app_config

logger = logging.getLogger("claude-proxy")


async def writeback_credential_dirs(session_id: str) -> None:
    """Copy bound-account credential files back to central source on session close.

    Iterates every path_env entry with role 'credentials_dir' on each MCP
    assigned to the session's agent. For OAuth-flagged MCPs, the per-session
    refreshed tokens are copied back to the central OAuth store so future
    sessions inherit the new tokens.

    Multi-account semantics:
      * The credential resolver places ONLY the bound account's token file
        in the per-session credentials_dir, so the copy targets exactly
        that one file by name.
      * The writeback acquires ``credential_locks.get_lock(user_sub, mcp,
        account_label)`` so concurrent refresh-worker writes don't
        interleave with the writeback (refresh-token rotation safety).

    Best-effort: catches all exceptions so it never blocks session cleanup.
    """
    try:
        from core.session.session_state import get_session_security
        from core.credentials import credential_locks
        from services.mcp import mcp_registry
        from services.oauth import oauth_account_store, credential_resolver
        from storage import database as task_store

        ctx = get_session_security(session_id)
        if not ctx:
            return

        # Visibility-modes: a Shared-only human chat has ``ctx.username`` set but
        # mounts the AGENT scope — its OAuth tokens are the agent's SERVICE
        # account, not the human's. Gate the user-vs-service writeback on the
        # MOUNT scope, never on username presence.
        is_user_scope = ctx.session_scope == "user"

        # Resolve the owning user_sub (for lock key + account binding).
        user_sub = ""
        if is_user_scope and ctx.username:
            with __import__("storage.pg", fromlist=["get_conn"]).get_conn() as conn:
                row = conn.execute(
                    "SELECT sub FROM users WHERE username = %s", (ctx.username,),
                ).fetchone()
                user_sub = row["sub"] if row else ""

        # Configuration view: consider every assigned MCP regardless of device
        # placement (non-OAuth ones skip below anyway), so a remote session's
        # OAuth MCPs still get their tokens written back.
        for manifest in (mcp_registry.get_agent_mcps_all_placements(ctx.agent) or []):
            # Only OAuth MCPs have central dirs for token writeback.
            if not manifest.credentials.oauth:
                continue

            provider_id = manifest.credentials.oauth.get("provider_id", "")
            if not provider_id:
                continue

            cred_entries = mcp_registry.get_credentials_dirs(manifest.name)
            if not cred_entries:
                continue

            # Resolve which account this session was bound to (the file
            # in the dir IS that account's token file).
            if is_user_scope and ctx.username and user_sub:
                ref = credential_resolver.pick_account(
                    manifest.name, ctx.agent, user_sub=user_sub,
                )
                if ref is None:
                    continue
                account_label = ref.label
                central_dir = oauth_account_store.get_token_dir(
                    ctx.username, provider_id=provider_id,
                )
            else:
                # Agent-scope session bound to a user's own account — writeback
                # to that user's token dir so the next user-scope chat (and the
                # next agent-scope run) both see the refreshed token.
                ref = credential_resolver.pick_account(
                    manifest.name, ctx.agent,
                )
                if ref is None:
                    continue
                account_label = ref.label
                bound_username = task_store.get_username_by_sub(ref.owner_sub) or ""
                if not bound_username:
                    continue
                central_dir = oauth_account_store.get_token_dir(
                    bound_username, provider_id=provider_id,
                )

            # Lock key is provider-scoped (multiple MCPs of the same
            # provider share the token file + grant). See
            # proxy/services/oauth_refresh_worker for the contract.
            lock = credential_locks.get_lock(
                user_sub or "_service", provider_id, account_label,
            )
            async with lock:
                # Same per-session destination layout the resolver copies
                # into (path_roles "credentials_dir": user dir for user-scope
                # sessions, knowledge/ for agent-scope).
                for _env_var, subpath in cred_entries:
                    if is_user_scope and ctx.username:
                        agent_dir = (
                            app_config.AGENTS_DIR / ctx.agent / "users"
                            / ctx.username / ".credentials" / subpath
                        )
                    else:
                        agent_dir = (
                            app_config.AGENTS_DIR / ctx.agent / "knowledge"
                            / ".credentials" / subpath
                        )
                    if not agent_dir.is_dir():
                        continue
                    # Copy back via to_thread (shutil.copy2 is sync I/O).
                    await asyncio.to_thread(
                        _copy_account_files, agent_dir, central_dir,
                        account_label,
                    )

        logger.debug("Credential dir writeback done for session %s", session_id[:8])
    except Exception:
        logger.debug(
            "Credential dir writeback skipped for session %s",
            session_id[:8], exc_info=True,
        )


def _copy_account_files(agent_dir, central_dir, account_label: str) -> None:
    """Copy the bound account's token file by name. Both user and service
    scopes now use the ``{account_label}.json`` convention.

    Sync — call from ``asyncio.to_thread``.
    """
    import shutil
    central_dir.mkdir(parents=True, exist_ok=True)
    src = agent_dir / f"{account_label}.json"
    if src.exists():
        shutil.copy2(src, central_dir / src.name)
