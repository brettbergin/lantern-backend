"""Grants over the API: an owner writes the standing rules that let agents
take decisions, everyone with ``audit:read`` can read them and the ledger
of what was decided, and nobody else — no admin, no plain client that
reads as an owner, no agent — can write one."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lantern.api.admin import ADMIN_ACTIONS
from lantern.daemon.controls import ControlError, ControlService, Principal
from lantern.daemon.controls.delegation import DELEGABLE_ACTIONS, Decision
from lantern.daemon.controls.delegation_store import DelegationStore
from tests.api.conftest import Api, build
from tests.api.test_role_grants import _register_member, _register_owner

GRANT = {
    "agent_slug": "critic",
    "action": "plan.approve",
    "conditions": {"repositories": ["o/r"], "max_children": 8, "require_review": True},
    "daily_limit": 5,
    "note": "small reviewed levels",
}
WRITE = frozenset({"policy:manage", "audit:read"})
READ = frozenset({"audit:read"})


def _headers(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _create(api: Api, headers: dict[str, str], **changed: Any) -> Any:
    return api.client.post("/v1/grants", json={**GRANT, **changed}, headers=headers)


def _notices(api: Api, kind: str) -> list[str]:
    events = api.client.get("/v1/events", headers=api.bearer()).json()["data"]
    return [
        str(e["data"]["text"])
        for e in events
        if e["type"] == "daemon.notice" and e["data"].get("kind") == kind
    ]


class TestAnOwnerManagesGrants:
    def test_grants_ship_empty(self, api: Api) -> None:
        listed = api.client.get("/v1/grants", headers=api.bearer(READ))
        assert listed.status_code == 200, listed.text
        assert listed.json() == {"data": [], "next_cursor": None, "has_more": False}
        assert api.loop.delegation.grants() == []

    def test_an_owner_creates_reads_edits_and_deletes_a_grant(self, api: Api) -> None:
        owner = _headers(_register_owner(api))
        created = _create(api, owner)
        assert created.status_code == 201, created.text
        body = created.json()
        grant = body["grant"]
        assert grant["id"].startswith("grant_") and grant["revision"] == 1
        assert grant["agent_slug"] == "critic" and grant["action"] == "plan.approve"
        assert grant["conditions"] == {
            "repositories": ["o/r"],
            "levels": None,
            "max_children": 8,
            "require_review": True,
            "causes": None,
            "max_retries": None,
        }
        assert grant["daily_limit"] == 5 and grant["enabled"] is True
        assert grant["used_today"] == 0
        assert grant["note"] == "small reviewed levels"
        assert grant["created_by_display"] == "owner" and grant["created_by"]
        assert grant["created_at"] == grant["updated_at"] and grant["created_at"].endswith("Z")
        assert body["operation"]["action"] == "grant.create"
        assert body["operation"]["state"] == "succeeded"
        assert body["operation"]["target"] == {"kind": "grant", "id": grant["id"]}
        assert "critic" in body["message"] and "plan.approve" in body["message"]

        assert api.client.get("/v1/grants", headers=owner).json()["data"] == [grant]
        assert api.client.get(f"/v1/grants/{grant['id']}", headers=owner).json() == grant

        edited = api.client.patch(
            f"/v1/grants/{grant['id']}",
            json={
                "expected_revision": 1,
                "daily_limit": None,
                "conditions": {"levels": ["task"]},
                "enabled": False,
            },
            headers=owner,
        )
        assert edited.status_code == 200, edited.text
        changed = edited.json()["grant"]
        assert changed["revision"] == 2 and changed["daily_limit"] is None
        assert changed["enabled"] is False and changed["note"] == "small reviewed levels"
        assert changed["conditions"]["levels"] == ["task"]
        assert changed["conditions"]["repositories"] is None
        assert edited.json()["operation"]["action"] == "grant.update"

        stale = api.client.patch(
            f"/v1/grants/{grant['id']}",
            json={"expected_revision": 1, "note": "from a stale read"},
            headers=owner,
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "stale_revision"
        assert stale.json()["current_revision"] == 2
        assert api.client.get(f"/v1/grants/{grant['id']}", headers=owner).json() == changed

        removed = api.client.delete(f"/v1/grants/{grant['id']}", headers=owner)
        assert removed.status_code == 200, removed.text
        assert removed.json()["grant"] is None
        assert removed.json()["operation"]["action"] == "grant.delete"
        assert api.client.get("/v1/grants", headers=owner).json()["data"] == []
        assert api.client.get(f"/v1/grants/{grant['id']}", headers=owner).status_code == 404

    def test_a_grant_that_is_not_there_is_a_plain_404(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        assert api.client.get("/v1/grants/grant_nope", headers=headers).status_code == 404
        patched = api.client.patch(
            "/v1/grants/grant_nope", json={"expected_revision": 1, "note": "x"}, headers=headers
        )
        assert patched.status_code == 404
        assert api.client.delete("/v1/grants/grant_nope", headers=headers).status_code == 404

    def test_an_edit_names_the_revision_it_read_and_changes_something(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        grant = _create(api, headers).json()["grant"]
        missing = api.client.patch(
            f"/v1/grants/{grant['id']}", json={"note": "no revision"}, headers=headers
        )
        assert missing.status_code == 422
        empty = api.client.patch(
            f"/v1/grants/{grant['id']}", json={"expected_revision": 1}, headers=headers
        )
        assert empty.status_code == 422
        identity = api.client.patch(
            f"/v1/grants/{grant['id']}",
            json={"expected_revision": 1, "action": "plan.publish"},
            headers=headers,
        )
        assert identity.status_code == 422

    def test_the_same_key_creates_one_grant(self, api: Api) -> None:
        headers = {**api.bearer(WRITE), "Idempotency-Key": "grant-1"}
        first = _create(api, headers)
        again = _create(api, headers)
        assert first.status_code == 201 and again.status_code == 201
        assert again.json()["grant"]["id"] == first.json()["grant"]["id"]
        assert again.json()["operation"]["id"] == first.json()["operation"]["id"]
        assert len(api.loop.delegation.grants()) == 1
        clash = _create(api, headers, daily_limit=9)
        assert clash.status_code == 409 and clash.json()["code"] == "idempotency_conflict"

    def test_each_write_is_narrated_and_recorded(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        grant = _create(api, headers).json()["grant"]
        api.client.patch(
            f"/v1/grants/{grant['id']}", json={"expected_revision": 1, "note": "x"}, headers=headers
        )
        api.client.delete(f"/v1/grants/{grant['id']}", headers=headers)
        operations = api.client.get(
            f"/v1/operations?target_kind=grant&target_id={grant['id']}", headers=headers
        ).json()["data"]
        # The harness clock stands still, so the three share a timestamp.
        by_action = {op["action"]: op for op in operations}
        assert sorted(by_action) == ["grant.create", "grant.delete", "grant.update"]
        assert len(operations) == 3
        assert {op["state"] for op in operations} == {"succeeded"}
        assert all(op["effect"] for op in operations)
        assert by_action["grant.create"]["request"] == {
            "agent_slug": "critic",
            "action": "plan.approve",
            "conditions": {"repositories": ["o/r"], "max_children": 8, "require_review": True},
            "daily_limit": 5,
            "enabled": True,
            "note": "small reviewed levels",
        }
        assert by_action["grant.update"]["request"] == {"changes": {"note": "x"}}
        assert by_action["grant.delete"]["request"] == {
            "agent_slug": "critic",
            "action": "plan.approve",
        }
        for kind in ("daemon.grant_added", "daemon.grant_updated", "daemon.grant_removed"):
            (text,) = _notices(api, kind)
            assert "critic" in text and "plan.approve" in text and "tester" in text
        types = [
            e["type"] for e in api.client.get("/v1/events", headers=api.bearer()).json()["data"]
        ]
        assert types.count("operation.accepted") == types.count("operation.finished") == 3

    def test_use_today_is_counted_from_the_ledger(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        grant = _create(api, headers).json()["grant"]
        store: DelegationStore = api.loop.delegation
        for _ in range(2):
            store.record(
                Decision(outcome="allow", grant_id=grant["id"], reason="allowed"),
                agent_slug="critic",
                action="plan.approve",
                attrs={"repository": "o/r"},
                now=api.clock(),
            )
        (listed,) = api.client.get("/v1/grants", headers=headers).json()["data"]
        assert listed["used_today"] == 2
        assert (
            api.client.get(f"/v1/grants/{grant['id']}", headers=headers).json()["used_today"] == 2
        )


class TestWhoMayWrite:
    def test_an_admin_reads_and_is_refused_every_write(self, api: Api) -> None:
        owner = _headers(_register_owner(api))
        admin = _headers(_register_member(api, "ada", role="admin"))
        grant = _create(api, owner).json()["grant"]
        assert api.client.get("/v1/grants", headers=admin).json()["data"] == [grant]
        assert api.client.get(f"/v1/grants/{grant['id']}", headers=admin).status_code == 200
        assert api.client.get("/v1/decisions", headers=admin).status_code == 200
        for refused in (
            _create(api, admin),
            api.client.patch(
                f"/v1/grants/{grant['id']}",
                json={"expected_revision": 1, "enabled": False},
                headers=admin,
            ),
            api.client.delete(f"/v1/grants/{grant['id']}", headers=admin),
        ):
            assert refused.status_code == 403, refused.text
            assert refused.json()["code"] == "forbidden"
            assert refused.json()["capability"] == "policy:manage"
            assert "policy:manage" in refused.json()["detail"]
        assert api.client.get("/v1/grants", headers=owner).json()["data"] == [grant]

    def test_a_member_neither_reads_nor_writes(self, api: Api) -> None:
        owner = _headers(_register_owner(api))
        member = _headers(_register_member(api))
        grant = _create(api, owner).json()["grant"]
        for read in ("/v1/grants", f"/v1/grants/{grant['id']}", "/v1/decisions"):
            refused = api.client.get(read, headers=member)
            assert refused.status_code == 403 and refused.json()["capability"] == "audit:read"
        for refused in (
            _create(api, member),
            api.client.patch(
                f"/v1/grants/{grant['id']}", json={"expected_revision": 1}, headers=member
            ),
            api.client.delete(f"/v1/grants/{grant['id']}", headers=member),
        ):
            assert refused.status_code == 403
            assert refused.json()["capability"] == "policy:manage"

    def test_a_client_that_reads_as_an_owner_is_refused(self, api: Api) -> None:
        """``daemon:manage`` makes a plain client an "owner" where a route
        asks for a role. Grants ask for the capability, never the role."""
        operator = api.bearer(frozenset({"daemon:manage", "audit:read", "runs:read"}))
        grant = _create(api, api.bearer(WRITE)).json()["grant"]
        assert api.client.get("/v1/grants", headers=operator).status_code == 200
        for refused in (
            _create(api, operator),
            api.client.patch(
                f"/v1/grants/{grant['id']}",
                json={"expected_revision": 1, "enabled": False},
                headers=operator,
            ),
            api.client.delete(f"/v1/grants/{grant['id']}", headers=operator),
        ):
            assert refused.status_code == 403
            assert refused.json()["capability"] == "policy:manage"
        assert len(api.loop.delegation.grants()) == 1

    def test_a_refused_write_leaves_no_record(self, api: Api) -> None:
        before = len(api.loop.operations.recent())
        assert _create(api, api.bearer(READ)).status_code == 403
        assert len(api.loop.operations.recent()) == before

    @pytest.mark.parametrize("on_behalf_of", [None, "Olive Owner"])
    def test_an_agent_principal_cannot_write_a_grant(
        self, api: Api, on_behalf_of: str | None
    ) -> None:
        """Whoever it is working for: the rules that say what an agent may
        decide are not an agent's to edit."""
        service = ControlService(api.loop)
        agent = Principal.for_agent("critic", on_behalf_of)
        existing = _create(api, api.bearer(WRITE)).json()["grant"]
        before = len(api.loop.operations.recent())
        attempts = (
            lambda: service.add_grant(
                agent,
                agent_slug="critic",
                action="plan.approve",
                conditions={},
                daily_limit=None,
                enabled=True,
                note=None,
            ),
            lambda: service.update_grant(
                agent, existing["id"], {"enabled": False}, expected_revision=1
            ),
            lambda: service.remove_grant(agent, existing["id"]),
        )
        for attempt in attempts:
            with pytest.raises(ControlError) as refused:
                attempt()
            assert refused.value.code == "forbidden"
            assert refused.value.detail["capability"] == "policy:manage"
        assert len(api.loop.operations.recent()) == before
        (kept,) = api.loop.delegation.grants()
        assert kept.id == existing["id"] and kept.enabled and kept.revision == 1


class TestWhatMayBeWritten:
    @pytest.mark.parametrize(
        ("changed", "field", "says"),
        [
            ({"agent_slug": "nobody"}, "agent_slug", "nobody"),
            ({"agent_slug": ""}, "agent_slug", "agent"),
            ({"action": "grant.update"}, "action", "cannot be delegated"),
            ({"action": "daemon.stop"}, "action", "cannot be delegated"),
            ({"action": "gate.approve"}, "action", "cannot be delegated"),
            (
                {"action": "plan.propose", "conditions": {"max_children": 3}},
                "conditions.max_children",
                "plan.propose",
            ),
            (
                {"action": "plan.run", "conditions": {"causes": ["timeout"]}},
                "conditions.causes",
                "plan.run",
            ),
            ({"conditions": {"levels": ["story"]}}, "conditions.levels", "story"),
            ({"conditions": {"repositories": []}}, "conditions.repositories", "repositories"),
        ],
    )
    def test_a_grant_that_makes_no_sense_is_a_422_naming_the_field(
        self, api: Api, changed: dict[str, Any], field: str, says: str
    ) -> None:
        headers = api.bearer(WRITE)
        refused = _create(api, headers, **changed)
        assert refused.status_code == 422, refused.text
        body = refused.json()
        assert body["code"] == "invalid_argument" and body["field"] == field
        assert says in body["detail"]
        assert api.loop.delegation.grants() == []
        # Refused before anything was recorded: there is nothing to reconcile.
        assert api.client.get("/v1/operations", headers=api.bearer()).json()["data"] == []

    @pytest.mark.parametrize(
        ("changed", "loc"),
        [
            ({"daily_limit": 0}, "daily_limit"),
            ({"daily_limit": -3}, "daily_limit"),
            ({"conditions": {"expression": "child_count < 3"}}, "expression"),
            ({"conditions": {"max_children": 0}}, "max_children"),
            ({"surprise": True}, "surprise"),
        ],
    )
    def test_a_malformed_body_names_the_field_too(
        self, api: Api, changed: dict[str, Any], loc: str
    ) -> None:
        refused = _create(api, api.bearer(WRITE), **changed)
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "invalid_request"
        assert any(loc in error["loc"] for error in refused.json()["errors"])
        assert api.loop.delegation.grants() == []

    def test_an_alias_or_a_disabled_agent_is_not_a_subject(self, tmp_path: Path) -> None:
        api = build(tmp_path, config={"agents": [{"slug": "critic", "enabled": False}]})
        with api.client:
            refused = _create(api, api.bearer(WRITE))
            assert refused.status_code == 422, refused.text
            assert refused.json()["field"] == "agent_slug"
            assert "disabled" in refused.json()["detail"]
            assert _create(api, api.bearer(WRITE), agent_slug="planner").status_code == 201
        api.ctx.close()

    def test_every_delegable_action_can_be_granted(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        for action in DELEGABLE_ACTIONS:
            created = _create(api, headers, action=action, conditions={})
            assert created.status_code == 201, created.text
        assert sorted(g.action for g in api.loop.delegation.grants()) == sorted(DELEGABLE_ACTIONS)

    def test_an_edit_is_checked_against_the_grants_own_action(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        grant = _create(api, headers, action="plan.propose", conditions={}).json()["grant"]
        refused = api.client.patch(
            f"/v1/grants/{grant['id']}",
            json={"expected_revision": 1, "conditions": {"require_review": True}},
            headers=headers,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["field"] == "conditions.require_review"
        assert "plan.propose" in refused.json()["detail"]

    def test_a_grant_cannot_be_switched_on_for_an_agent_that_is_off(self, api: Api) -> None:
        owner = _headers(_register_owner(api))
        scout = {"slug": "scout", "name": "Scout", "description": "Finds facts."}
        assert api.client.post("/v1/agents", json=scout, headers=owner).status_code == 201
        created = _create(api, owner, agent_slug="scout", enabled=False)
        assert created.status_code == 201, created.text
        grant = created.json()["grant"]
        off = api.client.patch(
            "/v1/agents/scout", json={"expected_revision": 1, "enabled": False}, headers=owner
        )
        assert off.status_code == 200, off.text
        on = api.client.patch(
            f"/v1/grants/{grant['id']}",
            json={"expected_revision": 1, "enabled": True},
            headers=owner,
        )
        assert on.status_code == 422, on.text
        assert on.json()["field"] == "agent_slug" and "disabled" in on.json()["detail"]
        # Anything else about it can still be edited, and it can be removed.
        noted = api.client.patch(
            f"/v1/grants/{grant['id']}",
            json={"expected_revision": 1, "note": "the scout is off"},
            headers=owner,
        )
        assert noted.status_code == 200, noted.text
        assert api.client.delete(f"/v1/grants/{grant['id']}", headers=owner).status_code == 200


class TestTheLedger:
    def _seed(self, api: Api) -> dict[str, str]:
        store: DelegationStore = api.loop.delegation
        base = api.clock()
        ids: dict[str, str] = {}
        rows: tuple[tuple[str, str, str | None, float], ...] = (
            ("allowed", "allow", "grant_a", 0.0),
            ("denied", "deny", None, 10.0),
            ("waiting", "escalate", None, 20.0),
            ("settled", "escalate", None, 30.0),
        )
        for name, outcome, grant_id, offset in rows:
            row = store.record(
                Decision(outcome=outcome, grant_id=grant_id, reason=f"{name} because"),  # type: ignore[arg-type]
                agent_slug="operator" if name == "waiting" else "critic",
                action="plan.approve",
                attrs={"repository": "o/r", "level": "epic", "child_count": 3},
                now=base + offset,
                plan_id="plan_1",
                node_id="node_1",
                run_id="r1234abcd" if name == "allowed" else None,
                operation_id="op_1" if name == "allowed" else None,
            )
            ids[name] = row.id
        store.resolve(ids["settled"], by="usr_owner", resolution="declined", now=base + 40.0)
        return ids

    def test_decisions_are_listed_newest_first_as_they_were_recorded(self, api: Api) -> None:
        ids = self._seed(api)
        listed = api.client.get("/v1/decisions", headers=api.bearer(READ))
        assert listed.status_code == 200, listed.text
        data = listed.json()["data"]
        assert [d["id"] for d in data] == [
            ids["settled"],
            ids["waiting"],
            ids["denied"],
            ids["allowed"],
        ]
        allowed = data[-1]
        assert allowed["id"].startswith("dec_")
        assert (allowed["outcome"], allowed["grant_id"]) == ("allow", "grant_a")
        assert (allowed["agent_slug"], allowed["action"]) == ("critic", "plan.approve")
        assert allowed["reason"] == "allowed because"
        assert (allowed["plan_id"], allowed["node_id"]) == ("plan_1", "node_1")
        assert allowed["run_id"] == "run_r1234abcd" and allowed["operation_id"] == "op_1"
        assert (allowed["item_id"], allowed["epic_run_id"]) == (None, None)
        assert allowed["repository"] == "o/r"
        assert allowed["attrs"] == {"repository": "o/r", "level": "epic", "child_count": 3}
        assert allowed["at"].endswith("Z")
        assert (allowed["resolved_at"], allowed["resolved_by"], allowed["resolution"]) == (
            None,
            None,
            None,
        )
        settled = data[0]
        assert settled["resolution"] == "declined" and settled["resolved_by"] == "usr_owner"
        assert settled["resolved_at"].endswith("Z")

    def test_the_ledger_filters(self, api: Api) -> None:
        ids = self._seed(api)
        headers = api.bearer(READ)

        def listed(query: str) -> list[str]:
            response = api.client.get(f"/v1/decisions?{query}", headers=headers)
            assert response.status_code == 200, response.text
            return [d["id"] for d in response.json()["data"]]

        assert listed("outcome=allow") == [ids["allowed"]]
        assert listed("outcome=escalate") == [ids["settled"], ids["waiting"]]
        assert listed("unresolved=true") == [ids["waiting"]]
        assert listed("agent=operator") == [ids["waiting"]]
        assert listed("agent=critic&outcome=deny") == [ids["denied"]]
        since = api.client.get("/v1/decisions", headers=headers).json()["data"][1]["at"]
        assert listed(f"since={since.replace('+', '%2B')}") == [ids["settled"], ids["waiting"]]
        assert listed(f"since={api.clock() + 15.0}") == [ids["settled"], ids["waiting"]]
        bad = api.client.get("/v1/decisions?outcome=maybe", headers=headers)
        assert bad.status_code == 422
        assert api.client.get("/v1/decisions?since=yesterday", headers=headers).status_code == 422

    def test_the_ledger_pages_with_a_cursor_bound_to_its_filters(self, api: Api) -> None:
        ids = self._seed(api)
        headers = api.bearer(READ)
        first = api.client.get("/v1/decisions?limit=3", headers=headers).json()
        assert [d["id"] for d in first["data"]] == [ids["settled"], ids["waiting"], ids["denied"]]
        assert first["has_more"] is True and first["next_cursor"]
        second = api.client.get(
            f"/v1/decisions?limit=3&cursor={first['next_cursor']}", headers=headers
        ).json()
        assert [d["id"] for d in second["data"]] == [ids["allowed"]]
        assert second["has_more"] is False and second["next_cursor"] is None
        crossed = api.client.get(
            f"/v1/decisions?outcome=allow&cursor={first['next_cursor']}", headers=headers
        )
        assert crossed.status_code == 400 and crossed.json()["code"] == "invalid_cursor"

    def test_an_item_is_named_by_its_public_id(self, api: Api) -> None:
        store: DelegationStore = api.loop.delegation
        store.record(
            Decision(outcome="escalate", reason="no grant"),
            agent_slug="operator",
            action="item.retry",
            attrs={"repository": "o/r", "failure_cause": "timeout", "retries": 0},
            now=api.clock(),
            item_id="gh:issue:7",
            epic_run_id="erun_1",
        )
        (row,) = api.client.get("/v1/decisions", headers=api.bearer(READ)).json()["data"]
        assert row["item_id"].startswith("itm_") and row["epic_run_id"] == "erun_1"
        again = api.client.get("/v1/decisions", headers=api.bearer(READ)).json()["data"][0]
        assert again["item_id"] == row["item_id"]


class TestWhatIsOffered:
    def test_the_feature_is_listed(self, api: Api) -> None:
        body = api.client.get("/v1/capabilities", headers=api.bearer(READ)).json()
        assert "delegation" in body["features"]

    def test_policy_is_not_edited_over_the_websocket(self) -> None:
        """An owner's chat turn or socket carries ``policy:manage``; editing
        policy from either is deliberately not offered."""
        assert not [action for action in ADMIN_ACTIONS if action.startswith("grant")]
        assert not [cap for cap, _route in ADMIN_ACTIONS.values() if cap == "policy:manage"]
