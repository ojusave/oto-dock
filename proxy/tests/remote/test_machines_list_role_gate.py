"""GET /v1/admin/remote-machines stays a hard 403 for non-admins.

The dashboard's useRemoteMachines() is role-gated client-side (it must never
poll this endpoint as a non-admin — repeated 403s can trip network IDS/IPS
signatures and get the whole flow blocked); this pins the server side so the
data itself keeps its admin gate and a client regression is a 403, not a leak.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth.providers import UserContext, get_current_user


def _app(role: str) -> FastAPI:
    from api.remote import remote_machines as rm

    user = UserContext(
        sub="user-sub-self", email="u@test.com", name="U",
        role=role, agents=[], agent_roles={},
    )

    async def _stub_user():
        return user

    app = FastAPI()
    app.include_router(rm.router)
    app.dependency_overrides[get_current_user] = _stub_user
    return app


@pytest.mark.parametrize("role", ["member", "creator", "manager"])
def test_list_machines_403_for_non_admin(role):
    resp = TestClient(_app(role)).get("/v1/admin/remote-machines")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Admin required"


def test_list_machines_200_for_admin(temp_db):
    resp = TestClient(_app("admin")).get("/v1/admin/remote-machines")
    assert resp.status_code == 200
    assert resp.json() == {"machines": []}
