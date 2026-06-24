"""TOTP 2FA lifecycle E2E + hardening regressions.

Covers the full self-service loop through the real endpoints — enroll →
login with 2FA → disable (password-confirmed) → re-enroll — plus the
hardening invariants: recovery codes carry ≥80 bits and are single-use,
and the 2FA step token is spent on successful use (replay must not mint
a second session).
"""

import pyotp
import pytest
from fastapi.testclient import TestClient

from app import app
from auth import totp as totp_mod
from auth.password import hash_password
from auth.providers import UserContext, get_current_user
from auth.rate_limiter import clear_rate_limit
from storage import database as db

client = TestClient(app)

_PW = "correct-horse-battery-staple-42"
_EMAIL = "totp@t.com"


@pytest.fixture(autouse=True)
def _fresh_buckets():
    """The in-memory limiter + used-jti set persist across tests in a worker."""
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    totp_mod._used_jtis.clear()
    yield
    for bucket in ("login", "2fa"):
        clear_rate_limit(bucket, "testclient")
    app.dependency_overrides.pop(get_current_user, None)


def _mk_user() -> str:
    sub = db.create_local_user(_EMAIL, "Totp", "Totp", "member", hash_password(_PW))

    async def _me():
        return UserContext(sub=sub, email=_EMAIL, name="Totp", role="member")

    app.dependency_overrides[get_current_user] = _me
    return sub


def _login() -> dict:
    resp = client.post("/auth/login/local", json={"email": _EMAIL, "password": _PW})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _enroll() -> dict:
    setup = client.post("/v1/users/me/totp/setup").json()
    code = pyotp.TOTP(setup["secret"]).now()
    resp = client.post("/v1/users/me/totp/verify", json={"code": code})
    assert resp.status_code == 200, resp.text
    return setup


def test_full_lifecycle_enroll_login_disable_reenroll():
    _mk_user()

    # Before enrollment: plain login, no 2FA step.
    data = _login()
    assert "requires_2fa" not in data
    assert data["user"]["email"] == _EMAIL

    # Enroll. Codes are dash-grouped, 80 bits (20 hex chars) each.
    setup = _enroll()
    assert len(setup["recovery_codes"]) == 10
    for rc in setup["recovery_codes"]:
        assert len(rc.replace("-", "")) == 20
        assert rc == rc.upper()

    # Login now requires the 2FA step; a valid TOTP code completes it.
    data = _login()
    assert data["requires_2fa"] is True
    step_token = data["totp_session_token"]
    code = pyotp.TOTP(setup["secret"]).now()
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": code})
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == _EMAIL
    assert "session" in resp.cookies

    # Disable: wrong password rejected, correct password disables.
    resp = client.request("DELETE", "/v1/users/me/totp", json={"password": "wrong"})
    assert resp.status_code == 401
    resp = client.request("DELETE", "/v1/users/me/totp", json={"password": _PW})
    assert resp.status_code == 200
    assert "requires_2fa" not in _login()

    # Re-enroll works from scratch.
    setup2 = _enroll()
    assert setup2["secret"] != setup["secret"]
    assert _login()["requires_2fa"] is True


def test_step_token_is_single_use():
    _mk_user()
    setup = _enroll()
    step_token = _login()["totp_session_token"]
    totp = pyotp.TOTP(setup["secret"])

    # A FAILED attempt must not consume the token — typos are retryable.
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": "000000"})
    assert resp.status_code == 401
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": totp.now()})
    assert resp.status_code == 200

    # Success spent it: replay with a valid code is rejected outright.
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step_token, "code": totp.now()})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_recovery_code_login_consumes_the_code():
    _mk_user()
    setup = _enroll()
    rc_dashed = setup["recovery_codes"][0]
    rc_plain = setup["recovery_codes"][1].replace("-", "").lower()

    # Dashed form works and is consumed.
    step = _login()["totp_session_token"]
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step, "code": rc_dashed})
    assert resp.status_code == 200

    # The SAME code again: invalid (consumed).
    step = _login()["totp_session_token"]
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step, "code": rc_dashed})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid 2FA code"

    # Canonicalization: undashed lowercase form of another code works too.
    step = _login()["totp_session_token"]
    resp = client.post("/auth/login/2fa", json={"totp_session_token": step, "code": rc_plain})
    assert resp.status_code == 200

    # Two codes spent → eight remain in the stored (encrypted) list.
    row = db.get_user_by_email(_EMAIL)
    remaining = totp_mod.decrypt_recovery_codes(row["totp_recovery_enc"])
    assert len(remaining) == 8


def test_recovery_code_entropy_and_canonical_hashing():
    codes = totp_mod.generate_recovery_codes()
    assert len(codes) == 10
    for c in codes:
        raw = c.replace("-", "")
        assert len(raw) == 20  # 80 bits
        int(raw, 16)  # hex

    hashed = totp_mod.hash_recovery_codes(codes)
    # All display forms of the same code hash identically.
    variants = [codes[0], codes[0].lower(), codes[0].replace("-", ""), codes[0].replace("-", " ")]
    for v in variants:
        matched, remaining = totp_mod.verify_recovery_code(v, hashed)
        assert matched and len(remaining) == 9
