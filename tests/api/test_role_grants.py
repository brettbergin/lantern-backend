"""A member's client holds what their role grants today, not what it
granted when they registered.

A member's API client stores its capabilities, written at registration and
rewritten on a role change. When a release adds a capability to a role
(``plans:create`` and ``plans:publish`` did), every member who registered
before it kept the old set: an owner signed in to Lantern was refused a plan
with "lacks plans:create". The API now brings every active member's client
up to its role's set when it starts, and tokens minted from it afterwards
carry the new capability.
"""

from __future__ import annotations

import json
from typing import Any

from lantern.api.collaboration import _capabilities_json
from lantern.daemon.controls.principal import ROLE_CAPABILITIES
from lantern.db.api_models import ClientRow
from tests.api.conftest import Api

OWNER = {
    "email": "owner@example.test",
    "username": "owner",
    "password": "correct horse battery staple",
    "full_name": "Olive Owner",
}
BEFORE_PLANNING = {
    role: caps - {"plans:create", "plans:publish"} for role, caps in ROLE_CAPABILITIES.items()
}


def _register_owner(api: Api) -> dict[str, Any]:
    response = api.client.post("/v1/auth/local/register", json=OWNER)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _register_member(api: Api, username: str = "bob") -> dict[str, Any]:
    owner = api.ctx.collaboration.user_by_username("owner")
    assert owner is not None
    _, raw = api.ctx.collaboration.create_invite(
        "member", f"{username}@example.test", created_by=owner.id, ttl_s=3600, now=api.clock()
    )
    response = api.client.post(
        "/v1/auth/local/register",
        json={
            "email": f"{username}@example.test",
            "username": username,
            "password": "another long password",
            "full_name": "Bob Builder",
            "invite_token": raw,
        },
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _age(api: Api, username: str, role: str) -> None:
    """Rewrite the user's client as a release before planning left it."""
    user = api.ctx.collaboration.user_by_username(username)
    assert user is not None
    with api.ctx.collaboration.dstore.transaction() as session:
        client = session.get(ClientRow, user.client_id)
        assert client is not None
        client.capabilities_json = _capabilities_json(BEFORE_PLANNING[role])  # type: ignore[index]


def _stored(api: Api, username: str) -> set[str]:
    user = api.ctx.collaboration.user_by_username(username)
    assert user is not None
    with api.ctx.collaboration.dstore.read() as session:
        client = session.get(ClientRow, user.client_id)
        assert client is not None
        return set(json.loads(client.capabilities_json))


def _refresh(api: Api, tokens: dict[str, Any]) -> dict[str, Any]:
    response = api.client.post(
        "/v1/auth/token",
        json={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_members_registered_before_a_capability_existed_gain_it(api: Api) -> None:
    owner = _register_owner(api)
    _register_member(api)
    _age(api, "owner", "owner")
    _age(api, "bob", "member")
    assert "plans:create" not in _stored(api, "owner")

    updated = api.ctx.collaboration.sync_role_grants()

    assert updated == 2
    assert _stored(api, "owner") == set(ROLE_CAPABILITIES["owner"])
    assert _stored(api, "bob") == set(ROLE_CAPABILITIES["member"])
    fresh = _refresh(api, owner)
    headers = {"Authorization": f"Bearer {fresh['access_token']}"}
    me = api.client.get("/v1/me", headers=headers).json()
    assert {"plans:create", "plans:publish"} <= set(me["capabilities"])
    created = api.client.post(
        "/v1/plans",
        json={"level": "epic", "repository": "o/r", "title": "Contact section"},
        headers=headers,
    )
    assert created.status_code == 201, created.text


def test_a_second_sync_changes_nothing(api: Api) -> None:
    _register_owner(api)
    assert api.ctx.collaboration.sync_role_grants() == 0


def test_a_deactivated_member_is_not_given_anything(api: Api) -> None:
    _register_owner(api)
    _register_member(api)
    bob = api.ctx.collaboration.user_by_username("bob")
    owner = api.ctx.collaboration.user_by_username("owner")
    assert bob is not None and owner is not None
    api.ctx.collaboration.update_member(bob.id, active=False, now=api.clock())
    before = _stored(api, "bob")
    api.ctx.collaboration.sync_role_grants()
    assert _stored(api, "bob") == before
