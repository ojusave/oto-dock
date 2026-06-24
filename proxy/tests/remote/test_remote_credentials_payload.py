"""Remote OAuth file delivery — the start payload + fan-out registration.

The satellite writes each session's CLI credential file from the start
payload (``credentials_json`` for Claude, ``auth_json`` for Codex); no OAuth
token may ride the payload env (env is frozen at exec and outranks the
credential file, which would defeat rotation fan-out). These tests pin the
payload contract and the proxy-side subscription binding that makes remote
sessions visible to the turn guard + fan-out.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from core.execution_layer import AgentConfig
from core.remote.remote_execution import RemoteExecutionLayer


def _machine():
    return {
        "capabilities": json.dumps({"local_tunnel_port": 18400, "os": "linux"}),
        "pairing_scope": "admin",
    }


def _config(**overrides):
    base = dict(
        agent_name="test-agent",
        execution_target="machine-1",
        model="claude-sonnet-5",
        effort="high",
        permission_mode="auto",
        client_type="dashboard",
        extra_env={},
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.fixture()
def layer():
    return RemoteExecutionLayer(MagicMock())


async def _build(layer, config, execution_path):
    with patch("storage.remote_store.get_remote_machine", return_value=_machine()):
        return await layer._build_start_payload("sess-1", config, execution_path)


class TestClaudePayload:
    @pytest.mark.asyncio
    async def test_creds_blob_becomes_credentials_json_and_leaves_env(self, layer):
        blob = {"accessToken": "at", "refreshToken": "", "expiresAt": 5,
                "scopes": [], "subscriptionType": "", "rateLimitTier": ""}
        config = _config(extra_env={"_CLAUDE_CREDS_BLOB": json.dumps(blob)})
        payload = await _build(layer, config, "claude-code-cli")
        assert payload["credentials_json"] == {"claudeAiOauth": blob}
        # No token in the spawned env — the file is the only carrier.
        assert "_CLAUDE_CREDS_BLOB" not in payload["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in payload["env"]
        # The 401-recovery poll window bridges the WS propagation gap.
        assert payload["env"]["CLAUDE_CODE_OAUTH_401_WAIT_MS"] == "20000"

    @pytest.mark.asyncio
    async def test_api_key_session_has_no_credentials_json(self, layer):
        config = _config(extra_env={"ANTHROPIC_API_KEY": "sk-test"})
        payload = await _build(layer, config, "claude-code-cli")
        assert "credentials_json" not in payload
        assert "CLAUDE_CODE_OAUTH_401_WAIT_MS" not in payload["env"]
        assert payload["env"]["ANTHROPIC_API_KEY"] == "sk-test"


class TestCodexPayload:
    @pytest.mark.asyncio
    async def test_auth_json_carries_neutralized_refresh(self, layer):
        blob = {"auth_mode": "chatgpt",
                "tokens": {"id_token": "ID", "access_token": "OLD",
                           "refresh_token": "RFR", "account_id": "A"}}
        config = _config(extra_env={
            "_CODEX_OAUTH_TOKEN": "NEW",
            "_CODEX_AUTH_BLOB": json.dumps(blob),
        })
        payload = await _build(layer, config, "codex-cli")
        assert payload["auth_json"]["tokens"]["access_token"] == "NEW"
        assert payload["auth_json"]["tokens"]["refresh_token"] == ""
        assert "_CODEX_OAUTH_TOKEN" not in payload["env"]
        assert "_CODEX_AUTH_BLOB" not in payload["env"]


class TestBindSubscription:
    def setup_method(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        pool._session_subscriptions.clear()
        pool._session_token_expiry.clear()
        pool._issued_token_expiry.clear()
        token_fanout._targets.clear()

    def test_binds_and_registers_claude_target(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        config = _config(subscription_id="sub-1")
        payload = {"claude_dir_relative": "users/alice/.claude",
                   "credentials_json": {"claudeAiOauth": {}}}
        RemoteExecutionLayer._bind_subscription(
            "sess-1", config, "claude-code-cli", payload,
        )
        assert pool.get_session_subscription("sess-1") == "sub-1"
        target = token_fanout.session_target("sess-1")
        assert target.kind == "claude"
        assert target.machine_id == "machine-1"
        assert target.agent_name == "test-agent"
        assert target.dir_relative == "users/alice/.claude"

    def test_binds_and_registers_codex_target(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        config = _config(subscription_id="sub-2")
        payload = {"codex_dir_relative": "workspace/.codex",
                   "auth_json": {"tokens": {}}}
        RemoteExecutionLayer._bind_subscription(
            "sess-2", config, "codex-cli", payload,
        )
        assert pool.get_session_subscription("sess-2") == "sub-2"
        assert token_fanout.session_target("sess-2").kind == "codex"

    def test_api_key_session_binds_without_target(self):
        from services.engines import subscription_pool as pool
        from services.engines import token_fanout
        config = _config(subscription_id="sub-3")
        payload = {"claude_dir_relative": "workspace/.claude"}  # no credentials_json
        RemoteExecutionLayer._bind_subscription(
            "sess-3", config, "claude-code-cli", payload,
        )
        assert pool.get_session_subscription("sess-3") == "sub-3"
        assert token_fanout.session_target("sess-3") is None

    def test_no_subscription_is_a_noop(self):
        from services.engines import subscription_pool as pool
        config = _config(subscription_id="")
        RemoteExecutionLayer._bind_subscription(
            "sess-4", config, "claude-code-cli", {},
        )
        assert pool.get_session_subscription("sess-4") is None
