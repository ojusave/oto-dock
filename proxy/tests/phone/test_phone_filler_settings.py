"""Thinking-filler settings seeding: merged per-language lists + latency gate.

Fillers are one merged list per language, playback is latency-gated
(delay + repeat damper), and enable/disable lives per route — there are no
global enable keys to seed.
"""

from __future__ import annotations

import json

from services.phone.phone_config_seed import seed_phone_config
from storage import database as task_store


def test_seed_writes_merged_lists_and_latency_gates(temp_db):
    seed_phone_config()

    phrases = json.loads(task_store.get_platform_setting("phone_thinking_phrases"))
    assert all(isinstance(v, list) for v in phrases.values())
    assert phrases["en"] and phrases["el"]

    assert task_store.get_platform_setting("phone_thinking_filler_delay_s") == "0.5"
    assert task_store.get_platform_setting(
        "phone_thinking_filler_repeat_delay_s") == "2.0"
    # No global enable keys — the per-route toggles are authoritative.
    assert task_store.get_platform_setting("phone_backchannel_enabled") == ""
    assert task_store.get_platform_setting("phone_thinking_filler_enabled") == ""


def test_seed_preserves_admin_edited_values(temp_db):
    task_store.set_platform_setting(
        "phone_thinking_phrases", json.dumps({"en": ["righto"]}))
    task_store.set_platform_setting("phone_thinking_filler_delay_s", "1.25")

    seed_phone_config()

    assert json.loads(
        task_store.get_platform_setting("phone_thinking_phrases")) == {"en": ["righto"]}
    assert task_store.get_platform_setting("phone_thinking_filler_delay_s") == "1.25"
