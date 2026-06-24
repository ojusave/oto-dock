"""Delegation (Projects) storage columns.

chats.parent_chat_id / project_id / delegate_role and
dynamic_tasks.target_chat_id / max_runs / run_count / until_at — written by
the delegation spawn path and the continuation scheduler.
"""

import uuid

from storage import database as db


def _mk_chat(**kw) -> dict:
    chat_id = str(uuid.uuid4())
    return db.create_chat(chat_id, "user-1", "pa", **kw)


class TestChatDelegationColumns:
    def test_defaults_empty(self, temp_db):
        chat = _mk_chat()
        assert chat["parent_chat_id"] == ""
        assert chat["project_id"] == ""
        assert chat["delegate_role"] == ""

    def test_create_worker_chat(self, temp_db):
        parent = _mk_chat()
        worker = _mk_chat(
            origin="delegated",
            parent_chat_id=parent["id"],
            project_id="site-redesign",
            delegate_role="worker",
            title="Lane: header rework",
        )
        assert worker["origin"] == "delegated"
        assert worker["parent_chat_id"] == parent["id"]
        assert worker["project_id"] == "site-redesign"
        assert worker["delegate_role"] == "worker"
        assert worker["title"] == "Lane: header rework"

    def test_update_chat_stamps_orchestrator(self, temp_db):
        parent = _mk_chat()
        assert db.update_chat(
            parent["id"], project_id="site-redesign", delegate_role="orchestrator"
        )
        row = db.get_chat(parent["id"])
        assert row["project_id"] == "site-redesign"
        assert row["delegate_role"] == "orchestrator"


class TestDynamicTaskContinuationColumns:
    def _mk_task(self, **kw) -> dict:
        task_id = f"dyn-{uuid.uuid4().hex[:8]}"
        db.create_dynamic_task(
            task_id, "pa", "t", "do it", "cli", kw.pop("task_type", "one_time"),
            None, None, None, 600, "user-1", **kw,
        )
        return db.get_dynamic_task(task_id)

    def test_defaults(self, temp_db):
        task = self._mk_task()
        assert task["target_chat_id"] is None
        assert task["max_runs"] is None
        assert task["run_count"] == 0
        assert task["until_at"] is None

    def test_continuation_fields_persist(self, temp_db):
        task = self._mk_task(
            task_type="continuation",
            target_chat_id="chat-abc",
            max_runs=5,
            until_at="2026-07-09T00:00:00+00:00",
        )
        assert task["task_type"] == "continuation"
        assert task["target_chat_id"] == "chat-abc"
        assert task["max_runs"] == 5
        assert task["until_at"] == "2026-07-09T00:00:00+00:00"

    def test_increment_run_count(self, temp_db):
        task = self._mk_task(task_type="continuation", target_chat_id="chat-abc")
        assert db.increment_dynamic_task_run_count(task["id"]) == 1
        assert db.increment_dynamic_task_run_count(task["id"]) == 2
        assert db.get_dynamic_task(task["id"])["run_count"] == 2

    def test_increment_missing_row_returns_zero(self, temp_db):
        assert db.increment_dynamic_task_run_count("dyn-gone") == 0
