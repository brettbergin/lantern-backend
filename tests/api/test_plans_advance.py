"""A plan's ``advance`` switch, and who each node is from, over ``/v1/plans``.

``advance`` says whether a plan may move itself forward (``auto``) or waits
on a person at every step (``manual``, the default). Nothing acts on it
yet; what is settled here is who may flip it. A plan edit takes
``plans:create``, which members hold — but a plan that advances on its own
is published on its owner's say-so, so setting ``advance`` takes
``plans:publish``, on create and on edit. ``goal_id`` and a node's
``review`` are the daemon's to write and are read-only here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select

from lantern.daemon.controls.principal import ROLE_CAPABILITIES, Capability
from lantern.db.api_models import ApiEventRow
from lantern.plans.model import PlanReview, review_digest
from tests.api.conftest import Api
from tests.api.test_plans_publish import _forge

READ: frozenset[Capability] = frozenset({"runs:read"})
MEMBER: frozenset[Capability] = frozenset({"runs:read", "plans:create"})
ADMIN: frozenset[Capability] = frozenset({"runs:read", "plans:create", "plans:publish"})


def _post(api: Api, headers: dict[str, str], **body: Any) -> Any:
    payload = {"level": "epic", "repository": "o/r", "title": "An epic", **body}
    return api.client.post("/v1/plans", json=payload, headers=headers)


def _create(api: Api, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    response = _post(api, headers, **body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _patch(api: Api, headers: dict[str, str], plan: dict[str, Any], **body: Any) -> Any:
    return api.client.patch(
        f"/v1/plans/{plan['id']}",
        json={"expected_revision": plan["revision"], **body},
        headers=headers,
    )


def _read(api: Api, plan: dict[str, Any]) -> dict[str, Any]:
    return dict(api.client.get(f"/v1/plans/{plan['id']}", headers=api.bearer(READ)).json())


def _events(api: Api, type_: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each event of ``type_`` as its data and its actor."""
    with api.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
        )
        return [(json.loads(row.data_json), json.loads(row.actor_json or "{}")) for row in rows]


def _generate(api: Api, plan: dict[str, Any]) -> dict[str, Any]:
    """The root as a plan run generated it, so the plan can be published."""
    current = api.ctx.plans.get(plan["id"])
    generated = replace(current.root, **current.input, origin="planner")
    api.ctx.plans.store.apply(
        current.id, expected_revision=current.revision, now=api.clock(), upsert=[generated]
    )
    return _read(api, plan)


class TestWhoMaySetIt:
    def test_the_roles_agree_with_the_rule(self) -> None:
        assert "plans:publish" not in ROLE_CAPABILITIES["member"]
        assert ROLE_CAPABILITIES["member"] >= MEMBER
        for role in ("admin", "owner"):
            assert ROLE_CAPABILITIES[role] >= ADMIN  # type: ignore[index]

    def test_a_plan_is_manual_unless_someone_says_otherwise(self, api: Api) -> None:
        plan = _create(api, api.bearer(MEMBER))
        assert plan["advance"] == "manual" and plan["goal_id"] is None
        (listed,) = api.client.get("/v1/plans", headers=api.bearer(READ)).json()["data"]
        assert listed["advance"] == "manual" and listed["goal_id"] is None

    def test_an_admin_sets_it_on_create(self, api: Api) -> None:
        plan = _create(api, api.bearer(ADMIN), advance="auto")
        assert plan["advance"] == "auto" and plan["revision"] == 1
        assert _read(api, plan)["advance"] == "auto"
        (listed,) = api.client.get("/v1/plans", headers=api.bearer(READ)).json()["data"]
        assert listed["advance"] == "auto"

    def test_an_admin_flips_it_on_an_edit_and_it_is_an_event(self, api: Api) -> None:
        headers = api.bearer(ADMIN)
        plan = _create(api, headers)
        flipped = _patch(api, headers, plan, advance="auto")
        assert flipped.status_code == 200, flipped.text
        body = flipped.json()
        assert body["advance"] == "auto" and body["revision"] == plan["revision"] + 1
        changes = [(d, a) for d, a in _events(api, "plan.node.changed") if d["change"] == "advance"]
        ((data, actor),) = changes
        assert data == {
            "plan_id": plan["id"],
            "node_id": None,
            "change": "advance",
            "advance": "auto",
            "before": "manual",
        }
        assert actor["kind"] == "client" and actor["via"] == "api" and actor["id"]
        back = _patch(api, headers, body, advance="manual")
        assert back.status_code == 200 and back.json()["advance"] == "manual"

    def test_the_switch_and_the_sections_change_in_one_write(self, api: Api) -> None:
        headers = api.bearer(ADMIN)
        plan = _create(api, headers)
        edited = _patch(api, headers, plan, advance="auto", goal="sharper")
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["advance"] == "auto" and body["input"]["goal"] == "sharper"
        assert body["revision"] == plan["revision"] + 1

    def test_a_member_is_refused_on_create_naming_the_capability(self, api: Api) -> None:
        refused = _post(api, api.bearer(MEMBER), advance="auto")
        assert refused.status_code == 403, refused.text
        problem = refused.json()
        assert problem["code"] == "forbidden" and problem["capability"] == "plans:publish"
        assert "plans:publish" in problem["detail"]
        assert api.client.get("/v1/plans", headers=api.bearer(READ)).json()["data"] == []

    def test_a_member_is_refused_on_an_edit_and_nothing_is_written(self, api: Api) -> None:
        member = api.bearer(MEMBER)
        plan = _create(api, member)
        refused = _patch(api, member, plan, advance="auto", goal="sharper")
        assert refused.status_code == 403, refused.text
        problem = refused.json()
        assert problem["code"] == "forbidden" and problem["capability"] == "plans:publish"
        after = _read(api, plan)
        assert after["advance"] == "manual" and after["revision"] == plan["revision"]
        assert after["input"]["goal"] == ""

    def test_a_member_may_not_switch_it_off_either(self, api: Api) -> None:
        plan = _create(api, api.bearer(ADMIN), advance="auto")
        refused = _patch(api, api.bearer(MEMBER), plan, advance="manual")
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:publish"
        assert _read(api, plan)["advance"] == "auto"

    def test_a_members_other_edits_still_work(self, api: Api) -> None:
        member = api.bearer(MEMBER)
        plan = _create(api, api.bearer(ADMIN), advance="auto")
        edited = _patch(api, member, plan, goal="sharper")
        assert edited.status_code == 200, edited.text
        assert edited.json()["input"]["goal"] == "sharper" and edited.json()["advance"] == "auto"
        # Naming the value the plan already has asks for no change: a client
        # that echoes the whole plan back is not refused for it.
        echoed = _patch(api, member, edited.json(), advance="auto", goal="sharper still")
        assert echoed.status_code == 200, echoed.text
        assert echoed.json()["input"]["goal"] == "sharper still"
        assert _post(api, member, advance="manual", title="Another").status_code == 201
        assert not [d for d, _ in _events(api, "plan.node.changed") if d["change"] == "advance"]

    def test_naming_the_value_it_has_flips_nothing(self, api: Api) -> None:
        """It is the edit it would be without ``advance``: no switch event."""
        headers = api.bearer(ADMIN)
        plan = _create(api, headers)
        same = _patch(api, headers, plan, advance="manual")
        without = _patch(api, headers, same.json(), goal="")
        assert same.status_code == without.status_code == 200
        assert same.json()["advance"] == "manual"
        assert not [d for d, _ in _events(api, "plan.node.changed") if d["change"] == "advance"]

    def test_a_published_plan_can_still_be_switched(self, api: Api) -> None:
        headers = api.bearer(ADMIN)
        _forge(api)
        plan = _generate(api, _create(api, headers))
        published = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/publish",
            json={"expected_revision": plan["revision"]},
            headers={**headers, "Idempotency-Key": "k1"},
        )
        assert published.status_code == 200, published.text
        plan = published.json()["plan"]
        assert plan["state"] == "published"
        flipped = _patch(api, headers, plan, advance="auto")
        assert flipped.status_code == 200, flipped.text
        assert flipped.json()["advance"] == "auto"

    def test_a_stale_revision_and_an_unknown_value_are_refused(self, api: Api) -> None:
        headers = api.bearer(ADMIN)
        plan = _create(api, headers)
        stale = _patch(api, headers, {**plan, "revision": plan["revision"] + 3}, advance="auto")
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        assert _patch(api, headers, plan, advance="always").status_code == 422
        assert _post(api, headers, advance="always").status_code == 422
        assert _read(api, plan)["advance"] == "manual"


class TestWhatIsReadOnly:
    @pytest.mark.parametrize(
        "field",
        [{"goal_id": "goal_1"}, {"review": {"verdict": "approve"}}, {"proposed_by": "usr_x"}],
        ids=["goal_id", "review", "proposed_by"],
    )
    def test_the_daemons_fields_are_not_written_through_the_api(
        self, api: Api, field: dict[str, Any]
    ) -> None:
        headers = api.bearer()
        assert _post(api, headers, **field).status_code == 422
        plan = _create(api, headers)
        assert _patch(api, headers, plan, goal="sharper", **field).status_code == 422
        added = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={
                "expected_revision": plan["revision"],
                "parent_id": plan["root_id"],
                "title": "A task",
                **field,
            },
            headers=headers,
        )
        assert added.status_code == 422
        after = _read(api, plan)
        assert after["revision"] == plan["revision"] and after["goal_id"] is None
        assert after["nodes"][0]["review"] is None


class TestTheNode:
    def test_who_proposed_approved_and_published_is_on_the_node(self, api: Api) -> None:
        headers = api.bearer(ADMIN)
        _forge(api)
        plan = _generate(api, _create(api, headers))
        root = plan["nodes"][0]
        creator = root["proposed_by"]
        assert creator and root["approved_by"] is None and root["published_by"] is None
        assert root["review"] is None
        added = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={
                "expected_revision": plan["revision"],
                "parent_id": plan["root_id"],
                "title": "A task",
                "kind": "code",
            },
            headers=headers,
        ).json()
        approved = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/approve",
            json={"expected_revision": added["revision"]},
            headers=headers,
        )
        assert approved.status_code == 200, approved.text
        task = next(n for n in approved.json()["nodes"] if n["title"] == "A task")
        approver = task["approved_by"]
        assert approver and task["proposed_by"] and task["published_by"] is None
        published = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/publish",
            json={"expected_revision": approved.json()["revision"]},
            headers={**headers, "Idempotency-Key": "k1"},
        )
        assert published.status_code == 200, published.text
        nodes = {n["title"]: n for n in published.json()["plan"]["nodes"]}
        publisher = nodes["A task"]["published_by"]
        assert publisher and nodes["An epic"]["published_by"] == publisher
        assert nodes["A task"]["approved_by"] == approver
        # Each is the id the same act's event names as its actor.
        actors = {d["change"]: a["id"] for d, a in _events(api, "plan.node.changed")}
        assert actors["approved"] == approver and actors["published"] == publisher

    def test_a_review_is_read_with_whether_it_is_still_current(self, api: Api) -> None:
        headers = api.bearer(ADMIN)
        plan = _create(api, headers)
        added = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={
                "expected_revision": plan["revision"],
                "parent_id": plan["root_id"],
                "title": "A task",
                "kind": "code",
            },
            headers=headers,
        ).json()
        current = api.ctx.plans.get(plan["id"])
        review = PlanReview(
            run_id="r1review",
            verdict="escalate",
            reasons=("the task has no acceptance criteria",),
            digest=review_digest(current, current.root),
            reviewed_by="agent:critic",
            at=api.clock(),
        )
        api.ctx.plans.store.apply(
            current.id,
            expected_revision=current.revision,
            now=api.clock(),
            upsert=[replace(current.root, review=review)],
        )
        read = _read(api, plan)
        got = read["nodes"][0]["review"]
        assert got == {
            "run_id": "run_r1review",
            "verdict": "escalate",
            "reasons": ["the task has no acceptance criteria"],
            "digest": review.digest,
            "reviewed_by": "agent:critic",
            "at": got["at"],
            "current": True,
        }
        assert got["at"].endswith("Z")
        task = next(n for n in added["nodes"] if n["title"] == "A task")
        edited = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{task['id']}",
            json={"expected_revision": read["revision"], "goal": "something else"},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["nodes"][0]["review"]["current"] is False


class TestAdvertised:
    def test_planning_advance_is_a_feature(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer(READ)).json()["features"]
        assert "planning" in features and "planning.advance" in features
