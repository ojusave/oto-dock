"""Admin require-2FA policy: setting round-trip + forced-enrollment flag.

The policy never locks anyone out — enforcement is the ``must_enroll_2fa``
flag (login response + /auth/me) that routes the dashboard to the enrollment
screen. OIDC accounts are exempt (their IdP owns MFA), and the operator
forced-settings overlay pins the knob on managed installs.
"""

import pyotp
import pytest
from fastapi.testclient import TestClient

import config
from app import app
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_PW = "correct-horse-battery-staple-77"
_EMAIL = "req2fa@t.com"


@pytest.fixture(autouse=True)
def _fresh():
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    yield
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    app.dependency_overrides.pop(get_current_user, None)


def _mk_user(auth_provider: str = "local") -> str:
    sub = db.create_local_user(_EMAIL, "U", "U", "member", hash_password(_PW))
    if auth_provider != "local":
        db.update_user_auth_fields(sub, auth_provider=auth_provider)

    async def _me():
        return UserContext(sub=sub, email=_EMAIL, name="U", role="member",
                           auth_provider=auth_provider)

    app.dependency_overrides[get_current_user] = _me
    return sub


def _as_admin():
    async def _admin():
        return UserContext(sub="local:admin", email="a@t.com", name="A", role="admin")

    app.dependency_overrides[get_current_user] = _admin


def test_setting_roundtrip_defaults_off():
    _as_admin()
    assert client.get("/v1/admin/platform-settings").json()["require_2fa"] is False

    resp = client.put("/v1/admin/platform-settings", json={"require_2fa": True})
    assert resp.status_code == 200
    assert client.get("/v1/admin/platform-settings").json()["require_2fa"] is True

    client.put("/v1/admin/platform-settings", json={"require_2fa": False})
    assert client.get("/v1/admin/platform-settings").json()["require_2fa"] is False


def test_local_user_without_second_factor_must_enroll():
    _mk_user()
    db.set_platform_setting("require_2fa", "1")

    data = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert data["user"]["must_enroll_2fa"] is True
    # The session IS issued (no lockout) — /auth/me carries the flag too.
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is True


def test_flag_clears_after_totp_enrollment():
    _mk_user()
    db.set_platform_setting("require_2fa", "1")

    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    assert client.post("/v1/users/me/totp/verify", json={"code": code}).status_code == 200
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_policy_off_means_no_enrollment_flag():
    _mk_user()
    data = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert "must_enroll_2fa" not in data["user"]
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_oidc_user_exempt():
    _mk_user(auth_provider="oidc:authentik")
    db.set_platform_setting("require_2fa", "1")
    assert client.get("/auth/me").json()["user"]["must_enroll_2fa"] is False


def test_forced_settings_overlay_pins_the_knob(monkeypatch):
    monkeypatch.setattr(config, "_FORCED_SETTINGS", {"require_2fa": "1"})
    _as_admin()

    # Admin write is ignored; the read overlay keeps the forced value and the
    # key is surfaced as forced so the UI locks the control.
    client.put("/v1/admin/platform-settings", json={"require_2fa": False})
    data = client.get("/v1/admin/platform-settings").json()
    assert data["require_2fa"] is True
    assert "require_2fa" in data["forced_keys"]

    # Enforcement follows the forced value.
    _mk_user()
    login = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW}).json()
    assert login["user"]["must_enroll_2fa"] is True
