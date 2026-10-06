"""A member's client holds what their role grants today, not what it
granted when they registered.

A member's API client stores its capabilities, written at registration and
rewritten on a role change. When a release adds a capability to a role
(``plans:create`` and ``plans:publish`` did), every member who registered
before it kept the old set: an owner signed in to Lantern was refused a plan
with "lacks plans:create". The API now brings every active member's client
up to its role's set when it starts, and tokens minted from it afterwards
carry the new capability.

The same pass is what keeps a capability a role does *not* hold away from
it: ``policy:manage`` (editing the standing rules that let agents take
decisions) is the owner's alone, so an admin's client is left exactly as it
was and an owner's gains it.
"""

from __future__ import annotations

import json
from typing import Any

from lantern.api.collaboration import _capabilities_json
from lantern.daemon.controls.principal import ALL_CAPABILITIES, ROLE_CAPABILITIES
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
#: What each role's client held in the release before ``policy:manage``: an
#: owner everything there was, an admin all of that but ``credentials:manage``.
BEFORE_POLICY = {
    "owner": ALL_CAPABILITIES - {"policy:manage"},
    "admin": ALL_CAPABILITIES - {"policy:manage", "credentials:manage"},
}


def _register_owner(api: Api) -> dict[str, Any]:
    response = api.client.post("/v1/auth/local/register", json=OWNER)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _register_member(api: Api, username: str = "bob", role: str = "member") -> dict[str, Any]:
    owner = api.ctx.collaboration.user_by_username("owner")
    assert owner is not None
    _, raw = api.ctx.collaboration.create_invite(
        role,  # type: ignore[arg-type]
        f"{username}@example.test",
        created_by=owner.id,
        ttl_s=3600,
        now=api.clock(),
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


def _age(api: Api, username: str, role: str, held: Any = None) -> None:
    """Rewrite the user's client as an earlier release left it: before
    planning, unless ``held`` says which release's sets to use."""
    user = api.ctx.collaboration.user_by_username(username)
    assert user is not None
    with api.ctx.collaboration.dstore.transaction() as session:
        client = session.get(ClientRow, user.client_id)
        assert client is not None
        client.capabilities_json = _capabilities_json((held or BEFORE_PLANNING)[role])


def _stored(api: Api, username: str) -> set[str]:
    user = api.ctx.collaboration.user_by_username(username)
    assert user is not None
    with api.ctx.collaboration.dstore.read() as session:
        client = session.get(ClientRow, user.client_id)
        assert client is not None
        return set(json.loads(client.capabilities_json))


def _me(api: Api, tokens: dict[str, Any]) -> set[str]:
    """What the token carries now, as the API reports it."""
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = api.client.get("/v1/me", headers=headers)
    assert response.status_code == 200, response.text
    return set(response.json()["capabilities"])


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


# -- policy:manage: the owner's alone ------------------------------------------------


def test_an_owner_gains_policy_manage_at_start_and_an_admin_is_left_as_it_was(api: Api) -> None:
    owner = _register_owner(api)
    admin = _register_member(api, "ada", role="admin")
    _age(api, "owner", "owner", BEFORE_POLICY)
    _age(api, "ada", "admin", BEFORE_POLICY)

    updated = api.ctx.collaboration.sync_role_grants()

    # Only the owner's client changed: the admin's row already held exactly
    # what an admin holds, so it lost nothing and gained nothing.
    assert updated == 1
    assert _stored(api, "owner") == set(ALL_CAPABILITIES)
    assert _stored(api, "ada") == set(BEFORE_POLICY["admin"])
    assert "policy:manage" in _me(api, _refresh(api, owner))
    assert _me(api, _refresh(api, admin)) == set(BEFORE_POLICY["admin"])


def test_an_admin_client_that_held_policy_manage_loses_it_at_start(api: Api) -> None:
    """However the row came to hold it, the role's set is what is written
    back, and a token minted while it did is narrowed on its next request."""
    _register_owner(api)
    admin = _register_member(api, "ada", role="admin")
    ada = api.ctx.collaboration.user_by_username("ada")
    assert ada is not None
    with api.ctx.collaboration.dstore.transaction() as session:
        client = session.get(ClientRow, ada.client_id)
        assert client is not None
        client.capabilities_json = _capabilities_json(ALL_CAPABILITIES)
    wide = _refresh(api, admin)
    assert "policy:manage" in wide["scope"].split()
    assert "policy:manage" in _me(api, wide)

    assert api.ctx.collaboration.sync_role_grants() == 1

    assert _stored(api, "ada") == set(ROLE_CAPABILITIES["admin"])
    assert "policy:manage" not in _me(api, wide)
    assert "policy:manage" not in _refresh(api, wide)["scope"].split()


def test_an_owner_made_an_admin_loses_policy_manage_on_a_live_token(api: Api) -> None:
    _register_owner(api)
    second = _register_member(api, "ada", role="owner")
    assert "policy:manage" in second["scope"].split()
    assert "policy:manage" in _me(api, second)
    ada = api.ctx.collaboration.user_by_username("ada")
    assert ada is not None

    api.ctx.collaboration.set_role(ada.id, "admin")

    assert _me(api, second) == set(ROLE_CAPABILITIES["admin"])
    assert "policy:manage" not in _me(api, second)
    # Nothing an admin held before the capability existed went with it.
    assert _stored(api, "ada") == set(BEFORE_POLICY["admin"])


def test_an_invited_admin_and_member_never_hold_policy_manage(api: Api) -> None:
    _register_owner(api)
    admin = _register_member(api, "ada", role="admin")
    member = _register_member(api, "bob")
    assert "policy:manage" not in admin["scope"].split()
    assert "policy:manage" not in member["scope"].split()
    assert "policy:manage" not in _stored(api, "ada") | _stored(api, "bob")
    assert "policy:manage" in _stored(api, "owner")
