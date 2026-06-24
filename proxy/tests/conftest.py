"""Shared test fixtures for proxy tests.

Uses a DEDICATED PostgreSQL test database (otodock_test) to avoid
destroying production data. Each test gets a clean slate via TRUNCATE.

IMPORTANT: Tests NEVER run against the production database.
Set TEST_DATABASE_URL to override the test DB location.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse as _urlparse, urlunparse as _urlunparse

import pytest

# Add proxy root to sys.path so imports work like they do in production
_PROXY_DIR = Path(__file__).parent.parent
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))

def _ensure_test_database(url: str) -> None:
    """Create the target test database if missing and mark it disposable.

    Connects to the ``postgres`` maintenance DB on the same server. The test DB
    is throwaway, so ``synchronous_commit`` is turned off (durability we don't
    need) to drop the per-commit fsync. Guarded to only ever touch ``*_test*``
    databases, so it can never create/alter the real dev database. If the
    maintenance DB is unreachable (locked-down role), we assume the DB was
    provisioned out of band (dev-setup / CI) and carry on.
    """
    import psycopg

    p = _urlparse(url)
    dbname = p.path.lstrip("/")
    assert "test" in dbname, f"refusing to manage non-test database {dbname!r}"
    admin_url = _urlunparse(p._replace(path="/postgres"))
    try:
        with psycopg.connect(admin_url, autocommit=True) as c:
            exists = c.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)
            ).fetchone()
            if not exists:
                c.execute(f'CREATE DATABASE "{dbname}"')
            c.execute(f'ALTER DATABASE "{dbname}" SET synchronous_commit TO off')
    except Exception:
        pass


# Force tests to use a separate database — NEVER the dev/production one.
# Default: same server as the dev DB but database name 'otodock_test'.
_test_url = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://otodock:otodock@localhost:5432/otodock_test",
)

# Under pytest-xdist each worker gets its OWN database (…_gw0, …_gw1, …) so
# parallel workers never share rows or block each other on the per-test wipe.
# Serial runs (no xdist) use the base name unchanged.
_xdist_worker = os.environ.get("PYTEST_XDIST_WORKER")
if _xdist_worker:
    _pr = _urlparse(_test_url)
    _test_url = _urlunparse(
        _pr._replace(path=f"/{_pr.path.lstrip('/')}_{_xdist_worker}")
    )

_ensure_test_database(_test_url)


def _assert_test_db_reachable(url: str) -> None:
    """Fail FAST when the test database can't be reached.

    Without this, an unreachable/wrong-credential test DB surfaces as
    psycopg_pool silently retrying the connection forever inside the first
    fixture — indistinguishable from a deadlock. Abort collection with the
    actual error and the knob to fix it instead.
    """
    import psycopg

    try:
        psycopg.connect(url, connect_timeout=5).close()
    except Exception as e:
        raise RuntimeError(
            f"test database unreachable: {url!r} → {e}\n"
            "Set TEST_DATABASE_URL to a reachable Postgres (a dedicated "
            "test instance; the DB name must contain 'test')."
        ) from e


_assert_test_db_reachable(_test_url)
os.environ["DATABASE_URL"] = _test_url

# Redirect AGENTS_DIR to a temp scratch path so tests that exercise the
# real filesystem (community-agent installer, agent-create endpoint) don't
# pollute the dev install. Tests that don't touch the FS aren't affected.
# Per-worker under xdist so parallel workers don't wipe each other's files
# (the temp_db fixture clears this dir every test).
import tempfile  # noqa: E402

_agents_dir_name = "otodock-test-agents"
if _xdist_worker:
    _agents_dir_name += f"-{_xdist_worker}"
_test_agents_root = Path(tempfile.gettempdir()) / _agents_dir_name
_test_agents_root.mkdir(parents=True, exist_ok=True)

# Override config.AGENTS_DIR directly — relying on PLATFORM_DATA_DIR alone
# doesn't help if config was already imported by another module's
# side-effect (e.g. via pre-conftest registration in pytest collection).
import config as _config  # noqa: E402
_config.AGENTS_DIR = _test_agents_root


def _truncate_all_tables(conn):
    """Wipe every row from the public-schema tables for a clean per-test slate.

    Uses DELETE, NOT ``TRUNCATE``: TRUNCATE forces a synchronous per-file fsync
    (``DataFileImmediateSync``) for every table it touches, and this fixture
    runs once per test (autouse), so on a durable server that per-table fsync
    dominates the whole suite's wall-clock. DELETE on these near-empty test
    tables skips it entirely. Behaviour matches the old TRUNCATE — all rows
    gone, sequences NOT reset (plain TRUNCATE also keeps sequences).

    FK ordering is sidestepped by disabling replication-role triggers for the
    duration of this transaction; ``SET LOCAL`` auto-resets on commit/rollback,
    so no state leaks back to the pooled connection.
    """
    rows = conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    ).fetchall()
    table_names = [r["tablename"] for r in rows]
    if not table_names:
        return
    # First statement opens the transaction; SET LOCAL is valid from here.
    conn.execute("SET LOCAL session_replication_role = 'replica'")
    for t in table_names:
        conn.execute(f'DELETE FROM "{t}"')


@pytest.fixture(autouse=True)
def temp_db():
    """Provide a clean database for each test.

    - On first call: ensures schema exists (idempotent)
    - Before each test: truncates all tables
    - Seeds minimal user data that most tests need
    """
    from storage import pg as pg_pool
    from storage import schema as pg_schema

    # Ensure schema exists (no-op if already created)
    with pg_pool.get_conn() as conn:
        pg_schema.init_schema(conn)
        pg_schema.run_migrations(conn)
        conn.commit()

    # Truncate all data for a clean slate
    with pg_pool.get_conn() as conn:
        _truncate_all_tables(conn)
        conn.commit()

    # Invalidate in-memory caches so post-TRUNCATE state is observed. The
    # agent_store cache is the load-bearing one — its API normally
    # invalidates itself on writes, but TRUNCATE goes around the API.
    try:
        from storage import agent_store as _agent_store
        _agent_store._invalidate_cache()
    except Exception:
        pass

    # Also wipe any agent directories left behind on the filesystem by
    # tests that exercise the real install path. Test AGENTS_DIR was
    # redirected to a tempdir at module import; we just clear its contents.
    import shutil
    try:
        import config as _cfg
        if _cfg.AGENTS_DIR.exists():
            for child in _cfg.AGENTS_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
    except Exception:
        pass

    from storage import database as db

    # Seed minimal user data
    _seed_users()

    yield db

    # No teardown needed — next test truncates


def _seed_users():
    """Insert test users into the DB."""
    from datetime import datetime, timezone
    from storage.pg import get_conn

    now = datetime.now(timezone.utc).isoformat()
    users = [
        ("user-admin", "admin@test.com", "Admin User", "admin", now, now),
        ("user-manager", "manager@test.com", "Manager User", "creator", now, now),
        ("user-viewer", "viewer@test.com", "Viewer User", "member", now, now),
        ("user-viewer2", "viewer2@test.com", "Viewer Two", "member", now, now),
    ]
    with get_conn() as conn:
        for sub, email, name, role, created_at, last_login in users:
            conn.execute(
                "INSERT INTO users (sub, email, name, role, created_at, last_login) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (sub, email, name, role, created_at, last_login),
            )
        conn.commit()
