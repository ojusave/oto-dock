"""Tests for the manifest `credentials.oauth` block validator
(`services/mcp_registry._validate_oauth_services`).

Strict validation protects against community MCPs shipping garbage scope
arrays that silently grant nothing (broken integration) or grant the
wrong scopes (security issue). Every test here is a contract guarantee
for community MCP authors.

An earlier revision was Google-specific. The validator was later generalized:
every oauth block must now declare ``provider_id``, and per-provider scope
regexes apply (Google's still strict; generic providers only check non-empty
strings).

The tests below stay Google-flavored — a tiny ``_validate`` wrapper
auto-injects ``provider_id: "google"`` so the test bodies stay focused
on the per-field semantics they exercise. Provider-id-specific tests
live in ``TestProviderId``.
"""

import pytest

from services.mcp.mcp_registry import _validate_oauth_services as _strict_validate


def _validate_oauth_services(raw, mcp_name):
    """Inject ``provider_id`` + ``flows`` for compact test fixtures.

    Most tests assert one specific field's validation rule and don't
    bother constructing a fully-valid wrapper. Tests that want to assert
    the provider-id-missing or flows-missing failure modes call
    ``_strict_validate`` directly.
    """
    if isinstance(raw, dict):
        if "provider_id" not in raw and "provider" not in raw:
            raw = {**raw, "provider_id": "google"}
        if "flows" not in raw:
            raw = {**raw, "flows": ["authorization_code"]}
    return _strict_validate(raw, mcp_name)


def _raw_validate(raw, mcp_name, server_raw=None):
    """Auto-inject ``flows`` for tests that already supply ``provider_id``.

    Lets tests focus on the field they're exercising without restating the
    full required minimum. Use ``_strict_validate`` directly when the
    intent is to assert a missing-required-field failure mode.
    """
    if isinstance(raw, dict) and "flows" not in raw:
        raw = {**raw, "flows": ["authorization_code"]}
    return _strict_validate(raw, mcp_name, server_raw)


# ═══════════════════════════════════════════════════════════════════════════
# Happy path — validator returns None and does not raise
# ═══════════════════════════════════════════════════════════════════════════


class TestValidOAuthBlock:
    def test_returns_none_when_omitted(self):
        assert _validate_oauth_services(None, "any") is None

    def test_minimal_valid_block_with_only_base_scopes(self):
        _validate_oauth_services({
            "base_scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
            ],
        }, "any")

    def test_minimal_valid_block_with_one_service(self):
        _validate_oauth_services({
            "services": [
                {
                    "key": "gmail",
                    "label": "Gmail",
                    "description": "Read emails",
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                }
            ],
        }, "any")

    def test_full_google_workspace_block(self):
        """Round-trip a representative subset of the actual google-workspace manifest."""
        _validate_oauth_services({
            "base_scopes": [
                "openid",
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
            ],
            "services": [
                {
                    "key": "gmail", "label": "Gmail",
                    "description": "Read and send emails",
                    "scopes": [
                        "https://www.googleapis.com/auth/gmail.readonly",
                        "https://www.googleapis.com/auth/gmail.send",
                    ],
                },
                {
                    "key": "drive", "label": "Drive",
                    "description": "Read and manage files",
                    "scopes": [
                        "https://www.googleapis.com/auth/drive",
                        "https://www.googleapis.com/auth/drive.file",
                    ],
                },
            ],
        }, "google-workspace")

    def test_empty_scopes_list_allowed(self):
        """OAuth-login-only services (no API scope) are allowed by the schema."""
        _validate_oauth_services({
            "services": [
                {
                    "key": "loginonly",
                    "label": "Login only",
                    "description": "OAuth identity, no API scopes",
                    "scopes": [],
                }
            ],
        }, "any")

    def test_extra_unknown_keys_ignored(self):
        """Validator only enforces required fields; extras don't raise."""
        _validate_oauth_services({
            "provider_id": "google",
            "base_scopes": ["openid"],
            "services": [
                {
                    "key": "gmail", "label": "Gmail", "description": "x",
                    "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
                    "future_field": "ignored",
                }
            ],
        }, "any")


# ═══════════════════════════════════════════════════════════════════════════
# Validator failures — must raise ValueError
# ═══════════════════════════════════════════════════════════════════════════


class TestInvalidOAuthBlock:
    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_oauth_services("nope", "x")

    def test_base_scopes_not_list(self):
        with pytest.raises(ValueError, match="base_scopes"):
            _validate_oauth_services({"base_scopes": "openid"}, "x")

    def test_base_scopes_empty_list(self):
        with pytest.raises(ValueError, match="base_scopes"):
            _validate_oauth_services({"base_scopes": []}, "x")

    def test_base_scope_non_string(self):
        with pytest.raises(ValueError, match=r"base_scopes\[0\]"):
            _validate_oauth_services({"base_scopes": [123]}, "x")

    def test_base_scope_empty_string(self):
        with pytest.raises(ValueError, match=r"base_scopes\[0\]"):
            _validate_oauth_services({"base_scopes": [""]}, "x")

    def test_base_scope_invalid_url(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services({"base_scopes": ["not-a-url"]}, "x")

    def test_base_scope_wrong_domain(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services(
                {"base_scopes": ["https://api.slack.com/scopes/chat:write"]}, "x"
            )

    def test_services_not_list(self):
        with pytest.raises(ValueError, match="services"):
            _validate_oauth_services({"services": "gmail"}, "x")

    def test_services_empty_list(self):
        with pytest.raises(ValueError, match="services"):
            _validate_oauth_services({"services": []}, "x")

    def test_service_entry_not_dict(self):
        with pytest.raises(ValueError, match=r"services\[0\]"):
            _validate_oauth_services({"services": ["gmail"]}, "x")

    def test_service_missing_key(self):
        with pytest.raises(ValueError, match=r"services\[0\].key"):
            _validate_oauth_services({"services": [
                {"label": "Gmail", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_empty_key(self):
        with pytest.raises(ValueError, match=r"services\[0\].key"):
            _validate_oauth_services({"services": [
                {"key": "", "label": "Gmail", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_missing_label(self):
        with pytest.raises(ValueError, match=r"services\[0\].label"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_empty_label(self):
        with pytest.raises(ValueError, match=r"services\[0\].label"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "", "description": "x", "scopes": []}
            ]}, "x")

    def test_service_missing_description(self):
        with pytest.raises(ValueError, match=r"services\[0\].description"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "scopes": []}
            ]}, "x")

    def test_service_missing_scopes_field(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x"}
            ]}, "x")

    def test_service_scopes_not_list(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": "gmail.readonly"}
            ]}, "x")

    def test_service_scope_non_string(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes\[0\]"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": [42]}
            ]}, "x")

    def test_service_scope_empty_string(self):
        with pytest.raises(ValueError, match=r"services\[0\].scopes\[0\]"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": [""]}
            ]}, "x")

    def test_service_scope_invalid_url(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x",
                 "scopes": ["readonly"]}
            ]}, "x")

    def test_service_scope_wrong_domain(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _validate_oauth_services({"services": [
                {"key": "x", "label": "X", "description": "x",
                 "scopes": ["https://example.com/scope/foo"]}
            ]}, "x")

    def test_duplicate_service_keys(self):
        with pytest.raises(ValueError, match="duplicates"):
            _validate_oauth_services({"services": [
                {"key": "gmail", "label": "Gmail", "description": "x", "scopes": []},
                {"key": "gmail", "label": "Mail2", "description": "y", "scopes": []},
            ]}, "x")


# ═══════════════════════════════════════════════════════════════════════════
# Boundary cases that should pass — openid as base, "openid" inside services
# ═══════════════════════════════════════════════════════════════════════════


class TestBoundaryAllowed:
    def test_openid_scope_in_base(self):
        _validate_oauth_services({"base_scopes": ["openid"]}, "x")

    def test_openid_scope_in_service(self):
        _validate_oauth_services({"services": [
            {"key": "x", "label": "X", "description": "x", "scopes": ["openid"]}
        ]}, "x")


# ═══════════════════════════════════════════════════════════════════════════
# Provider_id, generic provider acceptance, bearer_required,
# capabilities, token_format, refresh.
# ═══════════════════════════════════════════════════════════════════════════


class TestProviderId:
    def test_missing_provider_id_raises(self):
        with pytest.raises(ValueError, match="provider_id"):
            _raw_validate({"base_scopes": ["openid"]}, "x")

    def test_legacy_provider_field_rejected(self):
        with pytest.raises(ValueError, match="provider_id"):
            _raw_validate(
                {"provider": "google", "base_scopes": ["openid"]}, "x",
            )

    def test_unknown_provider_skips_scope_url_regex(self):
        """A provider_id without a hardcoded scope regex (generic
        providers) is accepted with arbitrary non-empty scope strings."""
        _raw_validate({
            "provider_id": "linear",
            "authorization_url": "https://linear.app/oauth/authorize",
            "token_url": "https://api.linear.app/oauth/token",
            "services": [{
                "key": "issues", "label": "Issues", "description": "Read issues",
                "scopes": ["read", "write"],
            }],
        }, "linear-mcp")

    def test_google_provider_still_enforces_strict_scopes(self):
        with pytest.raises(ValueError, match="not a valid google scope"):
            _raw_validate({
                "provider_id": "google",
                "services": [{
                    "key": "x", "label": "X", "description": "x",
                    "scopes": ["read"],
                }],
            }, "x")


class TestBearerRequired:
    def test_bearer_required_requires_proposed_hosts(self):
        with pytest.raises(ValueError, match="proposed_hosts"):
            _raw_validate(
                {"provider_id": "slack", "bearer_required": True},
                "slack-mcp",
            )

    def test_bearer_required_validates_hostname_format(self):
        with pytest.raises(ValueError, match="not a valid hostname"):
            _raw_validate(
                {
                    "provider_id": "slack",
                    "bearer_required": True,
                    "proposed_hosts": ["https://mcp.slack.com"],
                },
                "slack-mcp",
            )

    def test_bearer_required_cross_checks_transport(self):
        """When server.transport is stdio, bearer_required=true must reject."""
        with pytest.raises(ValueError, match="HTTP-class"):
            _raw_validate(
                {
                    "provider_id": "slack",
                    "bearer_required": True,
                    "proposed_hosts": ["mcp.slack.com"],
                },
                "slack-mcp",
                {"transport": "stdio"},
            )

    def test_bearer_required_accepts_http_transport(self):
        _raw_validate(
            {
                "provider_id": "slack",
                "bearer_required": True,
                "proposed_hosts": ["mcp.slack.com"],
            },
            "slack-mcp",
            {"transport": "streamable_http"},
        )

    def test_bearer_required_accepts_wildcard_host(self):
        _raw_validate(
            {
                "provider_id": "linear",
                "bearer_required": True,
                "proposed_hosts": ["*.linear.app"],
            },
            "linear-mcp",
            {"transport": "http"},
        )


class TestOptionalFields:
    def test_token_format_must_be_object(self):
        with pytest.raises(ValueError, match="token_format"):
            _raw_validate(
                {"provider_id": "google", "token_format": "workspace_mcp"},
                "x",
            )

    def test_token_format_schema_required_when_object(self):
        with pytest.raises(ValueError, match="token_format.schema"):
            _raw_validate(
                {"provider_id": "google", "token_format": {"schema": ""}},
                "x",
            )

    def test_refresh_strategy_rejects_unknown(self):
        with pytest.raises(ValueError, match="strategy"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "refresh": {"strategy": "aggressive"},
                },
                "x",
            )

    def test_refresh_min_remaining_must_be_positive(self):
        with pytest.raises(ValueError, match="min_remaining_seconds"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "refresh": {"strategy": "lazy", "min_remaining_seconds": -1},
                },
                "x",
            )

    def test_boolean_fields_type_checked(self):
        with pytest.raises(ValueError, match="supports_multi_account"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "supports_multi_account": "yes",
                },
                "x",
            )

    def test_capabilities_must_be_list_of_strings(self):
        with pytest.raises(ValueError, match="capabilities"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "services": [{
                        "key": "x", "label": "X", "description": "x",
                        "scopes": [],
                        "capabilities": "posts_as_other",
                    }],
                },
                "x",
            )

    def test_capabilities_accepted(self):
        _raw_validate(
            {
                "provider_id": "google",
                "services": [{
                    "key": "x", "label": "X", "description": "x",
                    "scopes": [],
                    "capabilities": ["posts_as_other_identity"],
                }],
            },
            "x",
        )

    def test_service_requires_admin_consent_type_checked(self):
        with pytest.raises(ValueError, match="requires_admin_consent"):
            _raw_validate(
                {
                    "provider_id": "google",
                    "services": [{
                        "key": "x", "label": "X", "description": "x",
                        "scopes": [],
                        "requires_admin_consent": "yes",
                    }],
                },
                "x",
            )
