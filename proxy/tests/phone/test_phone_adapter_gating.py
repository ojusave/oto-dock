"""Local-PBX adapter gating for cloud-prep.

``config.LOCAL_PBX_ENABLED`` (default ``not OTODOCK_CLOUD``, explicit
``OTODOCK_LOCAL_PBX_ENABLED`` override) removes the AudioSocket-based Asterisk /
FreePBX adapters from the creatable set — so OtoDock cloud can't add a local PBX
it could never reach, while self-host keeps them. Twilio/3CX stay available.
See config.py + proxy/api/phone/phone.py::create_phone_server.
"""

import config
from services.phone import phone_adapters


def test_all_adapters_available_when_local_pbx_enabled(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", True)
    types = set(phone_adapters.available_adapter_types())
    assert {"asterisk_manual", "asterisk_freepbx", "twilio", "three_cx"} <= types


def test_local_pbx_adapters_dropped_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", False)
    types = set(phone_adapters.available_adapter_types())
    # The AudioSocket/Asterisk family is gated off…
    assert "asterisk_manual" not in types
    assert "asterisk_freepbx" not in types
    # …while the cloud-reachable adapters remain.
    assert "twilio" in types
    assert "three_cx" in types


def test_available_is_subset_of_all_known_adapters(monkeypatch):
    # Whatever the flag, we never invent an adapter type that has no class.
    for enabled in (True, False):
        monkeypatch.setattr(config, "LOCAL_PBX_ENABLED", enabled)
        assert set(phone_adapters.available_adapter_types()) <= set(phone_adapters.ADAPTER_CLASSES)
