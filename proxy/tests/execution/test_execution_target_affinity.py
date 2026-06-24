"""Execution-target affinity: persistence round-trip.

A chat/run pins the execution target it ran on so resume never silently falls
back to a different target (which would lose the on-disk session/context the
agent built up on that machine).

These lock the schema + persistence layer. The pin branch in build_agent_config
(local stays local; offline machine_id → __offline__ sentinel → tailored error,
no fallback) is exercised end-to-end in the remote-satellite E2E.
"""


def test_chat_execution_target_defaults_local(temp_db):
    from storage import database as db
    db.create_chat("c1", "user-1", "agent-x")
    assert db.get_chat("c1")["execution_target"] == "local"


def test_chat_execution_target_persists(temp_db):
    """update_chat must allow execution_target. It was missing from the
    allowed-columns whitelist, which silently dropped the write — the pin would
    never stick and resume would re-resolve (the regression this guards)."""
    from storage import database as db
    db.create_chat("c2", "user-1", "agent-x")
    assert db.update_chat("c2", execution_target="machine-laptop") is True
    assert db.get_chat("c2")["execution_target"] == "machine-laptop"


def test_task_run_has_execution_target_column(temp_db):
    """task_runs pins identically (TaskRunView resume path)."""
    from storage.pg import get_conn
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO task_runs (id, task_id, agent, trigger_type, status) "
            "VALUES ('r1','t1','agent-x','manual','running')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT execution_target FROM task_runs WHERE id='r1'"
        ).fetchone()
    assert dict(row)["execution_target"] == "local"
