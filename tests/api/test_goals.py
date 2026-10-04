"""Goals over ``/v1/goals``: an owner or an admin sets the direction for a
repository, anyone who reads runs can read it, and a member cannot write
it. Each goal carries the plans proposed from it and the one currently
serving it."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lantern.daemon.controls.operations import EFFECTS
from lantern.daemon.controls.principal import Capability
from tests.api.conftest import Api, build
from tests.api.test_role_grants import _register_member, _register_owner

GOAL = {
    "repository": "o/r",
    "title": "Faster builds",
    "text": "Cut the build time in half without dropping a check.",
}
READ: frozenset[Capability] = frozenset({"runs:read"})
WRITE: frozenset[Capability] = frozenset({"runs:read", "plans:publish"})


def _headers(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def _create(api: Api, headers: dict[str, str], **changed: Any) -> Any:
    return api.client.post("/v1/goals", json={**GOAL, **changed}, headers=headers)


def _goal(api: Api, headers: dict[str, str], **changed: Any) -> dict[str, Any]:
    response = _create(api, headers, **changed)
    assert response.status_code == 201, response.text
    return dict(response.json()["goal"])


def _plan(api: Api, goal_id: str | None, title: str) -> str:
    """A plan proposed from ``goal_id``, as the daemon side creates one."""
    plan = api.ctx.plans.create(
        level="epic",
        repository="o/r",
        sections={"title": title},
        now=api.clock(),
        actor={"kind": "agent", "id": "agent:planner", "display": "planner"},
        goal_id=goal_id,
    )
    return plan.id


class TestAnOwnerSetsGoals:
    def test_goals_ship_empty_and_the_feature_is_listed(self, api: Api) -> None:
        listed = api.client.get("/v1/goals", headers=api.bearer(READ))
        assert listed.status_code == 200, listed.text
        assert listed.json() == {"data": [], "next_cursor": None, "has_more": False}
        features = api.client.get("/v1/capabilities", headers=api.bearer(READ)).json()["features"]
        assert "goals" in features

    def test_an_owner_creates_reads_edits_and_deletes_a_goal(self, api: Api) -> None:
        owner = _headers(_register_owner(api))
        created = _create(api, owner)
        assert created.status_code == 201, created.text
        body = created.json()
        goal = body["goal"]
        assert goal["id"].startswith("goal_") and goal["revision"] == 1
        assert goal["repository"] == "o/r" and goal["title"] == "Faster builds"
        assert goal["text"] == GOAL["text"] and goal["state"] == "active"
        assert goal["plans"] == [] and goal["open_plan_id"] is None
        assert goal["created_by_display"] == "owner" and goal["created_by"]
        assert goal["created_at"] == goal["updated_at"] and goal["created_at"].endswith("Z")
        assert body["operation"]["action"] == "goal.create"
        assert body["operation"]["state"] == "succeeded"
        assert body["operation"]["target"] == {"kind": "goal", "id": goal["id"]}
        assert api.client.get("/v1/goals", headers=owner).json()["data"] == [goal]
        assert api.client.get(f"/v1/goals/{goal['id']}", headers=owner).json() == goal

        edited = api.client.patch(
            f"/v1/goals/{goal['id']}",
            json={"expected_revision": 1, "state": "paused", "text": "Halve it."},
            headers=owner,
        )
        assert edited.status_code == 200, edited.text
        changed = edited.json()["goal"]
        assert changed["revision"] == 2 and changed["state"] == "paused"
        assert changed["text"] == "Halve it." and changed["title"] == "Faster builds"
        assert edited.json()["operation"]["action"] == "goal.update"

        stale = api.client.patch(
            f"/v1/goals/{goal['id']}",
            json={"expected_revision": 1, "state": "done"},
            headers=owner,
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "stale_revision"
        assert stale.json()["current_revision"] == 2
        assert api.client.get(f"/v1/goals/{goal['id']}", headers=owner).json() == changed

        removed = api.client.delete(f"/v1/goals/{goal['id']}", headers=owner)
        assert removed.status_code == 200, removed.text
        assert removed.json()["goal"] is None
        assert removed.json()["operation"]["action"] == "goal.delete"
        assert api.client.get("/v1/goals", headers=owner).json()["data"] == []
        assert api.client.get(f"/v1/goals/{goal['id']}", headers=owner).status_code == 404

    def test_an_admin_sets_a_goal_and_a_member_reads_it(self, api: Api) -> None:
        _register_owner(api)
        admin = _headers(_register_member(api, "ada", role="admin"))
        member = _headers(_register_member(api, "bob"))
        goal = _goal(api, admin)
        assert api.client.get("/v1/goals", headers=member).json()["data"] == [goal]
        assert api.client.get(f"/v1/goals/{goal['id']}", headers=member).status_code == 200

    def test_a_member_is_refused_every_write_naming_the_capability(self, api: Api) -> None:
        _register_owner(api)
        member = _headers(_register_member(api, "bob"))
        goal = _goal(api, api.bearer(WRITE))
        refusals = [
            _create(api, member),
            api.client.patch(
                f"/v1/goals/{goal['id']}",
                json={"expected_revision": 1, "state": "done"},
                headers=member,
            ),
            api.client.delete(f"/v1/goals/{goal['id']}", headers=member),
        ]
        for refused in refusals:
            assert refused.status_code == 403, refused.text
            problem = refused.json()
            assert problem["code"] == "forbidden" and problem["capability"] == "plans:publish"
            assert "plans:publish" in problem["detail"]
        assert api.client.get("/v1/goals", headers=api.bearer(READ)).json()["data"] == [goal]

    def test_a_goal_that_is_not_there_is_a_plain_404(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        assert api.client.get("/v1/goals/goal_nope", headers=headers).status_code == 404
        patched = api.client.patch(
            "/v1/goals/goal_nope", json={"expected_revision": 1, "state": "done"}, headers=headers
        )
        assert patched.status_code == 404
        assert api.client.delete("/v1/goals/goal_nope", headers=headers).status_code == 404

    def test_the_list_narrows_by_repository_and_state(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        active = _goal(api, headers)
        paused = _goal(api, headers, title="Docs", state="paused")
        listed = api.client.get("/v1/goals?state=paused", headers=headers).json()["data"]
        assert [g["id"] for g in listed] == [paused["id"]]
        by_repo = api.client.get("/v1/goals?repository=O/R", headers=headers).json()["data"]
        assert [g["id"] for g in by_repo] == [active["id"], paused["id"]]
        bad = api.client.get("/v1/goals?state=someday", headers=headers)
        assert bad.status_code == 422


class TestValidation:
    def test_a_repository_that_is_not_configured_is_named(self, api: Api) -> None:
        refused = _create(api, api.bearer(WRITE), repository="o/elsewhere")
        assert refused.status_code == 422, refused.text
        assert refused.json()["code"] == "invalid_argument"
        assert refused.json()["field"] == "repository"
        assert api.loop.goals.goals() == []

    def test_a_disabled_repository_or_one_that_cannot_hold_a_plan_is_named(
        self, tmp_path: Path
    ) -> None:
        built = build(
            tmp_path,
            config={
                "github": {
                    "repos": [
                        {"repo": "o/r"},
                        {"repo": "o/off", "enabled": False},
                        {"repo": "o/quiet", "planning": {"enabled": False}},
                    ]
                }
            },
        )
        with built.client:
            headers = built.bearer(WRITE)
            for repository, said in (("o/off", "disabled"), ("o/quiet", "planning is off")):
                refused = _create(built, headers, repository=repository)  # type: ignore[arg-type]
                assert refused.status_code == 422, refused.text
                problem = refused.json()
                assert problem["field"] == "repository" and said in problem["detail"]
            assert _create(built, headers).status_code == 201  # type: ignore[arg-type]
        built.ctx.close()

    def test_title_text_and_state_are_bounded(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        for changed in (
            {"title": ""},
            {"title": "x" * 201},
            {"text": ""},
            {"text": "x" * 4001},
            {"state": "someday"},
        ):
            assert _create(api, headers, **changed).status_code == 422, changed
        goal = _goal(api, headers)
        for body in (
            {"state": "done"},
            {"expected_revision": 1},
            {"expected_revision": 1, "repository": "o/other"},
            {"expected_revision": 1, "title": "   "},
        ):
            refused = api.client.patch(f"/v1/goals/{goal['id']}", json=body, headers=headers)
            assert refused.status_code == 422, (body, refused.text)
        blank = api.client.patch(
            f"/v1/goals/{goal['id']}",
            json={"expected_revision": 1, "title": "   "},
            headers=headers,
        )
        assert blank.json()["field"] == "title"
        assert api.client.get(f"/v1/goals/{goal['id']}", headers=headers).json() == goal


class TestPlansServingAGoal:
    def test_the_plans_proposed_from_a_goal_and_the_one_serving_it(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        goal = _goal(api, headers)
        other = _goal(api, headers, title="Docs")
        first = _plan(api, goal["id"], "First try")
        api.clock.t += 5  # type: ignore[attr-defined]
        second = _plan(api, goal["id"], "Second try")
        _plan(api, None, "A person's plan")
        current = api.ctx.plans.get(first)
        api.ctx.plans.store.apply(
            first, expected_revision=current.revision, now=api.clock(), archived=True
        )

        read = api.client.get(f"/v1/goals/{goal['id']}", headers=headers).json()
        # Archiving the first moved it last-changed; the second still serves.
        assert {p["plan_id"] for p in read["plans"]} == {first, second}
        by_id = {p["plan_id"]: p for p in read["plans"]}
        assert by_id[first]["state"] == "archived"
        assert by_id[second] == {
            "plan_id": second,
            "title": "Unplanned epic",
            "state": "draft",
            "advance": "manual",
        }
        assert read["open_plan_id"] == second
        listed = {g["id"]: g for g in api.client.get("/v1/goals", headers=headers).json()["data"]}
        assert listed[goal["id"]] == read
        assert listed[other["id"]]["plans"] == [] and listed[other["id"]]["open_plan_id"] is None
        plan = api.client.get(f"/v1/plans/{second}", headers=headers).json()
        assert plan["goal_id"] == goal["id"]

    def test_a_plan_route_does_not_take_a_goal(self, api: Api) -> None:
        headers = api.bearer(frozenset({"runs:read", "plans:create", "plans:publish"}))
        goal = _goal(api, headers)
        sent = api.client.post(
            "/v1/plans",
            json={"level": "epic", "repository": "o/r", "title": "x", "goal_id": goal["id"]},
            headers=headers,
        )
        # Extra fields are refused: a goal is the daemon's to name on a plan.
        assert sent.status_code == 422, sent.text
        assert api.client.get("/v1/plans", headers=headers).json()["data"] == []


class TestRecorded:
    def test_each_write_is_a_recorded_operation(self, api: Api) -> None:
        headers = api.bearer(WRITE)
        goal = _goal(api, headers)
        api.client.patch(
            f"/v1/goals/{goal['id']}",
            json={"expected_revision": 1, "state": "done"},
            headers=headers,
        )
        api.client.delete(f"/v1/goals/{goal['id']}", headers=headers)
        operations = api.client.get(
            f"/v1/operations?target_kind=goal&target_id={goal['id']}", headers=api.bearer()
        ).json()["data"]
        by_action = {op["action"]: op for op in operations}
        assert sorted(by_action) == ["goal.create", "goal.delete", "goal.update"]
        assert {op["state"] for op in operations} == {"succeeded"}
        assert all(op["effect"] == EFFECTS[op["action"]] for op in operations)
        assert by_action["goal.create"]["request"] == {
            "repository": "o/r",
            "title": "Faster builds",
            "state": "active",
        }
        assert by_action["goal.update"]["request"] == {"changes": {"state": "done"}}
        assert by_action["goal.delete"]["request"] == {
            "repository": "o/r",
            "title": "Faster builds",
        }

    def test_the_same_key_creates_one_goal(self, api: Api) -> None:
        headers = {**api.bearer(WRITE), "Idempotency-Key": "goal-1"}
        first = _create(api, headers)
        again = _create(api, headers)
        assert first.status_code == 201 and again.status_code == 201
        assert again.json()["goal"]["id"] == first.json()["goal"]["id"]
        assert len(api.loop.goals.goals()) == 1
