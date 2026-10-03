"""Plans: drafted, read and edited over ``/v1/plans`` (#2340).

Every plan, drafts included, is shared across the workspace: anyone who
may read runs reads every plan. Drafting and editing take ``plans:create``
(members hold it); publishing takes ``plans:publish`` (admins and owners).
Every mutation names the revision it read, and a stale one is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from lantern.daemon.controls.principal import ROLE_CAPABILITIES, Capability
from lantern.db.api_models import ApiEventRow
from lantern.plans.model import PlanNode
from tests.api.conftest import Api, build

READ: frozenset[Capability] = frozenset({"runs:read"})
DRAFT: frozenset[Capability] = frozenset({"runs:read", "plans:create"})
MANAGE: frozenset[Capability] = frozenset({"runs:read", "daemon:manage"})


def _create(api: Api, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"level": "initiative", "repository": "o/r", "title": "Plan the work", **body}
    response = api.client.post("/v1/plans", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _add(
    api: Api, headers: dict[str, str], plan: dict[str, Any], parent_id: str, **body: Any
) -> dict[str, Any]:
    response = api.client.post(
        f"/v1/plans/{plan['id']}/nodes",
        json={"expected_revision": plan["revision"], "parent_id": parent_id, **body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _node(plan: dict[str, Any], title: str) -> dict[str, Any]:
    return next(n for n in plan["nodes"] if n["title"] == title)


def _events(api: Api, type_: str) -> list[dict[str, Any]]:
    import json

    with api.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
        )
        return [json.loads(row.data_json) for row in rows]


class TestWhoMay:
    def test_members_draft_and_admins_and_owners_also_publish(self) -> None:
        assert "plans:create" in ROLE_CAPABILITIES["member"]
        assert "plans:publish" not in ROLE_CAPABILITIES["member"]
        for role in ("admin", "owner"):
            assert {"plans:create", "plans:publish"} <= ROLE_CAPABILITIES[role]  # type: ignore[index]

    def test_drafts_are_read_by_anyone_who_reads_runs(self, api: Api) -> None:
        plan = _create(api, api.bearer(DRAFT))
        reader = api.bearer(READ)
        listed = api.client.get("/v1/plans", headers=reader)
        assert listed.status_code == 200, listed.text
        assert [p["id"] for p in listed.json()["data"]] == [plan["id"]]
        assert listed.json()["data"][0]["state"] == "draft"
        read = api.client.get(f"/v1/plans/{plan['id']}", headers=reader)
        assert read.status_code == 200 and read.json()["input"]["title"] == "Plan the work"
        assert read.json()["nodes"][0]["title"] == "Unplanned initiative"

    def test_reading_alone_does_not_draft(self, api: Api) -> None:
        response = api.client.post(
            "/v1/plans",
            json={"level": "epic", "repository": "o/r", "title": "x"},
            headers=api.bearer(READ),
        )
        assert response.status_code == 403
        assert response.json()["capability"] == "plans:create"

    def test_the_capabilities_are_advertised(self, api: Api) -> None:
        body = api.client.get("/v1/capabilities", headers=api.bearer(READ)).json()
        assert {"plans:create", "plans:publish"} <= set(body["capabilities"])
        assert "planning" in body["features"]
        assert "planning.clarify" in body["features"]


class TestTheTree:
    def test_an_initiative_breaks_into_epics_and_an_epic_into_tasks(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, goal="ship planning", acceptance_criteria=["it works"])
        root = plan["nodes"][0]
        assert root["level"] == "initiative" and root["state"] == "draft"
        assert root["origin"] == "person" and root["acceptance_criteria"] == []
        assert plan["input"]["acceptance_criteria"] == ["it works"]
        plan = _add(api, headers, plan, root["id"], title="API")
        epic = _node(plan, "API")
        assert epic["level"] == "epic" and epic["repository"] == "o/r"
        plan = _add(api, headers, plan, epic["id"], title="Store", kind="code")
        plan = _add(
            api,
            headers,
            plan,
            epic["id"],
            title="Routes",
            kind="code",
            verify_commands=["make test"],
            depends_on=[_node(plan, "Store")["id"]],
        )
        store, routes = _node(plan, "Store"), _node(plan, "Routes")
        assert routes["depends_on"] == [store["id"]]
        assert [n["title"] for n in plan["nodes"]] == [
            "Unplanned initiative",
            "API",
            "Store",
            "Routes",
        ]
        assert plan["rollup"] == {"epics": 1, "tasks": 2, "tasks_closed": 0, "published": 0}
        assert plan["revision"] == 4

    def test_a_task_has_no_children_and_lives_in_its_epics_repository(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        epic = plan["nodes"][0]
        plan = _add(api, headers, plan, epic["id"], title="A task")
        task = _node(plan, "A task")
        under_task = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={"expected_revision": plan["revision"], "parent_id": task["id"], "title": "x"},
            headers=headers,
        )
        assert under_task.status_code == 422 and "no children" in under_task.json()["detail"]
        elsewhere = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={
                "expected_revision": plan["revision"],
                "parent_id": epic["id"],
                "title": "y",
                "repository": "o/other",
            },
            headers=headers,
        )
        assert elsewhere.status_code == 422 and "epic's repository" in elsewhere.json()["detail"]

    def test_only_a_task_carries_task_sections(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        response = api.client.post(
            f"/v1/plans/{plan['id']}/nodes",
            json={
                "expected_revision": plan["revision"],
                "parent_id": plan["root_id"],
                "title": "An epic",
                "kind": "code",
            },
            headers=headers,
        )
        assert response.status_code == 422 and "only a task" in response.json()["detail"]

    def test_dependencies_name_siblings_and_never_cycle(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        epic = plan["root_id"]
        plan = _add(api, headers, plan, epic, title="A")
        plan = _add(api, headers, plan, epic, title="B", depends_on=[_node(plan, "A")["id"]])
        a = _node(plan, "A")
        cycle = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{a['id']}",
            json={"expected_revision": plan["revision"], "depends_on": [_node(plan, "B")["id"]]},
            headers=headers,
        )
        assert cycle.status_code == 422 and "cycle" in cycle.json()["detail"]
        stranger = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{a['id']}",
            json={"expected_revision": plan["revision"], "depends_on": ["node_nowhere"]},
            headers=headers,
        )
        assert stranger.status_code == 422 and "sibling" in stranger.json()["detail"]

    def test_moving_a_node_renumbers_its_siblings(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        for title in ("A", "B", "C"):
            plan = _add(api, headers, plan, plan["root_id"], title=title)
        c = _node(plan, "C")
        moved = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{c['id']}",
            json={"expected_revision": plan["revision"], "position": 0},
            headers=headers,
        )
        assert moved.status_code == 200, moved.text
        tasks = [n for n in moved.json()["nodes"] if n["level"] == "task"]
        assert [(n["title"], n["position"]) for n in tasks] == [("C", 0), ("A", 1), ("B", 2)]

    def test_removing_a_node_takes_its_subtree_and_the_dependencies_on_it(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        plan = _add(api, headers, plan, plan["root_id"], title="A")
        plan = _add(
            api, headers, plan, plan["root_id"], title="B", depends_on=[_node(plan, "A")["id"]]
        )
        a = _node(plan, "A")
        removed = api.client.delete(
            f"/v1/plans/{plan['id']}/nodes/{a['id']}",
            params={"expected_revision": plan["revision"]},
            headers=headers,
        )
        assert removed.status_code == 200, removed.text
        (b,) = [n for n in removed.json()["nodes"] if n["level"] == "task"]
        assert b["title"] == "B" and b["depends_on"] == [] and b["position"] == 0


class TestRevisions:
    def test_a_stale_revision_is_refused_with_the_current_one(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        first = api.client.patch(
            f"/v1/plans/{plan['id']}",
            json={"expected_revision": plan["revision"], "goal": "one"},
            headers=headers,
        )
        assert first.status_code == 200 and first.json()["revision"] == plan["revision"] + 1
        stale = api.client.patch(
            f"/v1/plans/{plan['id']}",
            json={"expected_revision": plan["revision"], "goal": "two"},
            headers=headers,
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["code"] == "stale_revision"
        assert stale.json()["current_revision"] == plan["revision"] + 1
        assert (
            api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()["input"]["goal"]
            == "one"
        )

    @pytest.mark.parametrize("verb", ["add", "remove", "delete"])
    def test_every_mutation_checks_it(self, api: Api, verb: str) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        plan = _add(api, headers, plan, plan["root_id"], title="An epic")
        stale = plan["revision"] - 1
        epic = _node(plan, "An epic")
        if verb == "add":
            response = api.client.post(
                f"/v1/plans/{plan['id']}/nodes",
                json={"expected_revision": stale, "parent_id": epic["id"], "title": "t"},
                headers=headers,
            )
        elif verb == "remove":
            response = api.client.delete(
                f"/v1/plans/{plan['id']}/nodes/{epic['id']}",
                params={"expected_revision": stale},
                headers=headers,
            )
        else:
            response = api.client.delete(
                f"/v1/plans/{plan['id']}", params={"expected_revision": stale}, headers=headers
            )
        assert response.status_code == 409 and response.json()["code"] == "stale_revision"


class TestStates:
    def _mark(self, api: Api, plan_id: str, node_id: str, **changes: Any) -> None:
        from dataclasses import replace

        current = api.ctx.plans.get(plan_id)
        node = current.node(node_id)
        assert node is not None
        if node.id == current.root_id and changes.get("state") == "published" and current.input:
            node = replace(node, **current.input, origin="planner")
        edited: PlanNode = replace(node, **changes)
        api.ctx.plans.store.apply(
            plan_id, expected_revision=current.revision, now=api.clock(), upsert=[edited]
        )

    def test_editing_a_proposed_node_makes_it_a_draft(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        plan = _add(api, headers, plan, plan["root_id"], title="An epic")
        epic = _node(plan, "An epic")
        self._mark(api, plan["id"], epic["id"], state="proposed", origin="planner")
        current = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        edited = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{epic['id']}",
            json={"expected_revision": current["revision"], "goal": "sharper"},
            headers=headers,
        )
        assert edited.status_code == 200, edited.text
        node = _node(edited.json(), "An epic")
        assert node["state"] == "draft" and node["origin"] == "planner"

    def test_a_published_node_is_edited_on_the_forge_and_its_plan_is_archived(
        self, api: Api
    ) -> None:
        from lantern.plans.model import ForgeRef

        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        plan = _add(api, headers, plan, plan["root_id"], title="An epic")
        epic = _node(plan, "An epic")
        self._mark(
            api,
            plan["id"],
            epic["id"],
            state="published",
            forge=ForgeRef(number=12, url="https://github.com/o/r/issues/12", state="open"),
        )
        current = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        assert current["state"] == "published"
        forge = dict(_node(current, "An epic")["forge"])
        assert str(forge.pop("version")).startswith("c1-")
        assert forge == {
            "number": 12,
            "url": "https://github.com/o/r/issues/12",
            "state": "open",
            "updated_at": None,
            "detached": None,
            "marker_missing": False,
            "checklist_error": None,
        }
        refused = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{epic['id']}",
            json={"expected_revision": current["revision"], "title": "renamed"},
            headers=headers,
        )
        # Editing a published node writes its issue: drafting alone may not.
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:publish"
        archived = api.client.delete(
            f"/v1/plans/{plan['id']}",
            params={"expected_revision": current["revision"]},
            headers=headers,
        )
        assert archived.status_code == 200 and archived.json()["outcome"] == "archived"
        after = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        assert after["state"] == "archived"

    def test_a_draft_plan_is_deleted(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        deleted = api.client.delete(
            f"/v1/plans/{plan['id']}",
            params={"expected_revision": plan["revision"]},
            headers=headers,
        )
        assert deleted.status_code == 200 and deleted.json()["outcome"] == "deleted"
        assert api.client.get(f"/v1/plans/{plan['id']}", headers=headers).status_code == 404


class TestRepositories:
    def test_each_repository_says_how_it_holds_a_plan(self, api: Api) -> None:
        manage = api.bearer(MANAGE)
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "acme/widgets", "forge": "gitlab"},
            headers=manage,
        )
        assert created.status_code == 201, created.text
        listed = {
            r["repository"]: r["planning"]
            for r in api.client.get("/v1/repositories", headers=manage).json()["data"]
        }
        assert listed["o/r"] == {"hierarchy": "native", "reason": None}
        assert listed["acme/widgets"]["hierarchy"] == "checklist"
        assert "checklist" in listed["acme/widgets"]["reason"]

    def test_a_forge_that_cannot_hold_a_plan_is_named(self, api: Api) -> None:
        manage = api.bearer(MANAGE)
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "acme/gadgets", "forge": "gitea"},
            headers=manage,
        )
        assert created.status_code == 201, created.text
        listed = {
            r["repository"]: r["planning"]
            for r in api.client.get("/v1/repositories", headers=manage).json()["data"]
        }
        assert listed["acme/gadgets"] == {
            "hierarchy": "unsupported",
            "reason": "this repository's forge can't hold plans: Gitea is not supported",
        }
        refused = api.client.post(
            "/v1/plans",
            json={"level": "epic", "repository": "acme/gadgets", "title": "x"},
            headers=api.bearer(DRAFT),
        )
        assert refused.status_code == 409 and refused.json()["code"] == "planning_unsupported"
        assert "Gitea is not supported" in refused.json()["detail"]

    def test_an_unknown_repository_is_refused(self, api: Api) -> None:
        refused = api.client.post(
            "/v1/plans",
            json={"level": "epic", "repository": "nobody/nothing", "title": "x"},
            headers=api.bearer(DRAFT),
        )
        assert refused.status_code == 422 and refused.json()["code"] == "unknown_repository"

    def test_filters(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        epic = _create(api, headers, level="epic", title="lone epic")
        initiative = _create(api, headers, title="big one")
        by_level = api.client.get("/v1/plans", params={"level": "epic"}, headers=headers).json()
        assert [p["id"] for p in by_level["data"]] == [epic["id"]]
        by_repo = api.client.get("/v1/plans", params={"repository": "O/R"}, headers=headers).json()
        assert {p["id"] for p in by_repo["data"]} == {epic["id"], initiative["id"]}


class TestEvents:
    def test_creating_and_changing_a_plan_are_events(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        plan = _add(api, headers, plan, plan["root_id"], title="An epic")
        assert _events(api, "plan.created") == [
            {"plan_id": plan["id"], "level": "initiative", "repository": "o/r"}
        ]
        (changed,) = _events(api, "plan.node.changed")
        assert changed == {
            "plan_id": plan["id"],
            "node_id": _node(plan, "An epic")["id"],
            "change": "added",
        }


class TestSwitchedOff:
    def test_a_repository_with_planning_off_says_so_and_refuses_a_plan(
        self, tmp_path: Path
    ) -> None:
        built = build(
            tmp_path,
            config={
                "github": {
                    "repos": [{"repo": "o/r"}, {"repo": "o/quiet", "planning": {"enabled": False}}]
                }
            },
        )
        with built.client:
            headers = built.bearer(DRAFT)
            listed = {
                r["repository"]: r["planning"]
                for r in built.client.get("/v1/repositories", headers=headers).json()["data"]
            }
            assert listed["o/quiet"] == {
                "hierarchy": "unsupported",
                "reason": "planning is off for this repository ([planning] enabled = false)",
            }
            refused = built.client.post(
                "/v1/plans",
                json={"level": "epic", "repository": "o/quiet", "title": "x"},
                headers=headers,
            )
            assert refused.status_code == 409 and refused.json()["code"] == "planning_unsupported"
            features = built.client.get("/v1/capabilities", headers=headers).json()["features"]
            assert "planning" in features
        built.ctx.close()

    def test_planning_off_everywhere_is_not_offered(self, tmp_path: Path) -> None:
        built = build(tmp_path, config={"planning": {"enabled": False}})
        with built.client:
            features = built.client.get("/v1/capabilities", headers=built.bearer(READ)).json()[
                "features"
            ]
            assert "planning" not in features
            assert "planning.clarify" not in features
        built.ctx.close()


class TestBreakdown:
    """``POST .../nodes/{node_id}/breakdown`` queues a ``plan`` run (#2344)."""

    def _breakdown(
        self, api: Api, headers: dict[str, str], plan: dict[str, Any], node_id: str, **body: Any
    ) -> Any:
        return api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{node_id}/breakdown",
            json={"expected_revision": plan["revision"], **body},
            headers=headers,
        )

    def test_reading_alone_does_not_break_down(self, api: Api) -> None:
        plan = _create(api, api.bearer(DRAFT))
        refused = self._breakdown(api, api.bearer(READ), plan, plan["root_id"])
        assert refused.status_code == 403
        assert refused.json()["capability"] == "plans:create"

    def test_a_breakdown_queues_a_plan_run_for_the_node(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, title="Ship reports")
        accepted = self._breakdown(api, headers, plan, plan["root_id"], note="keep it to two epics")
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        assert body["plan_id"] == plan["id"] and body["node_id"] == plan["root_id"]
        assert body["created"] is True
        item = body["item"]
        assert item["kind"] == "plan" and item["state"] == "queued"
        assert item["title"] == "Generate the initiative and its epics from “Ship reports”"
        assert item["origin"]["kind"] == "api" and item["origin"]["repository"] == "o/r"
        assert item["run_id"] is None, "a run id is minted when the item is dispatched"
        assert body["operation"]["action"] == "item.admit"
        assert body["operation"]["state"] == "succeeded"
        # The row names its node and carries the note as its body.
        (row,) = [i for i in api.loop.dstore.items() if i.kind == "plan"]
        assert (row.plan_id, row.plan_node_id) == (plan["id"], plan["root_id"])
        assert row.body == "keep it to two epics" and row.repo == "o/r"
        # It is ordinary work: in the queue, and listed by kind.
        listed = api.client.get("/v1/items", params={"kind": "plan"}, headers=api.bearer()).json()
        assert [i["id"] for i in listed["data"]] == [item["id"]]
        # Admission changed nothing on the plan.
        after = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        assert after["revision"] == plan["revision"]

    def test_a_second_breakdown_waits_for_the_first(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        first = self._breakdown(api, headers, plan, plan["root_id"])
        assert first.status_code == 202, first.text
        again = self._breakdown(api, headers, plan, plan["root_id"])
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "already_in_progress"
        assert again.json()["plan_code"] == "generation_in_progress"

    def test_a_task_has_nothing_to_break_down(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        plan = _add(api, headers, plan, plan["root_id"], title="A task", kind="code")
        refused = self._breakdown(api, headers, plan, _node(plan, "A task")["id"])
        assert refused.status_code == 422 and "no children" in refused.json()["detail"]

    def test_a_published_node_with_children_on_the_forge_is_re_planned(self, api: Api) -> None:
        from lantern.plans.model import ForgeRef

        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        plan = _add(api, headers, plan, plan["root_id"], title="A task", kind="code")
        for node_id, number in ((plan["root_id"], 3), (_node(plan, "A task")["id"], 4)):
            TestStates()._mark(
                api,
                plan["id"],
                node_id,
                state="published",
                forge=ForgeRef(
                    number=number, url=f"https://github.com/o/r/issues/{number}", state="open"
                ),
            )
        current = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        accepted = self._breakdown(api, headers, current, current["root_id"])
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["item"]["title"] == "Re-plan the tasks of “An epic”"

    def test_a_published_node_whose_children_are_drafts_is_broken_down(self, api: Api) -> None:
        from lantern.plans.model import ForgeRef

        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="An epic")
        plan = _add(api, headers, plan, plan["root_id"], title="A task", kind="code")
        TestStates()._mark(
            api,
            plan["id"],
            plan["root_id"],
            state="published",
            forge=ForgeRef(number=3, url="https://github.com/o/r/issues/3", state="open"),
        )
        current = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        accepted = self._breakdown(api, headers, current, current["root_id"])
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["item"]["title"] == "Propose the tasks of “An epic”"

    def test_planning_switched_off_for_the_repository_refuses(self, api: Api) -> None:
        from lantern.config import PlanningConfig

        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        off = api.ctx.config.model_copy(update={"planning": PlanningConfig(enabled=False)})
        api.ctx.config = off
        api.loop.config = off
        refused = self._breakdown(api, headers, plan, plan["root_id"])
        assert refused.status_code == 409 and refused.json()["code"] == "planning_unsupported"
        assert "planning is off" in refused.json()["detail"]

    def test_a_full_level_and_a_stale_revision_are_refused(self, api: Api) -> None:
        from lantern.config import PlanningConfig
        from tests.api.test_plans_publish import _create as generated_plan

        headers = api.bearer(DRAFT)
        plan = generated_plan(api, headers, level="epic", title="An epic")
        plan = _add(api, headers, plan, plan["root_id"], title="Only", kind="code")
        stale = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/breakdown",
            json={"expected_revision": plan["revision"] - 1},
            headers=headers,
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        full = api.ctx.config.model_copy(update={"planning": PlanningConfig(max_tasks_per_epic=1)})
        api.ctx.config = full
        api.loop.config = full
        refused = self._breakdown(api, headers, plan, plan["root_id"])
        assert refused.status_code == 409 and refused.json()["code"] == "level_full"

    def test_an_unknown_channel_is_refused(self, api: Api) -> None:
        plan = _create(api, api.bearer(DRAFT))
        refused = self._breakdown(api, api.bearer(), plan, plan["root_id"], channel_id="chan_x")
        assert refused.status_code == 404 and refused.json()["code"] == "channel_not_found"
        assert [i for i in api.loop.dstore.items() if i.kind == "plan"] == []


class TestAnswers:
    """``POST .../nodes/{node_id}/answers`` answers or skips the questions a
    breakdown asked before it proposes, and resumes its run (#2345)."""

    QUESTIONS: tuple[dict[str, Any], ...] = (
        {
            "id": "fmt",
            "prompt": "Which formats?",
            "choices": [
                {"value": "csv", "label": "CSV", "description": "a spreadsheet"},
                {"value": "pdf", "label": "PDF"},
            ],
        },
        {
            "id": "who",
            "prompt": "Who downloads them?",
            "choices": ["staff", "public"],
            "allow_free_text": False,
        },
    )

    def _parked(self, api: Api) -> tuple[dict[str, Any], str, str]:
        """A plan whose breakdown is parked on two questions, as the daemon
        leaves it: the item waiting, its run pinned, the questions on the
        node. The plan as read, the item id and the run id."""
        from lantern.engine.planning import PlanQuestion

        headers = api.bearer(DRAFT)
        plan = _create(api, headers, level="epic", title="Export reports")
        accepted = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/breakdown",
            json={"expected_revision": plan["revision"]},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        (item,) = [i for i in api.loop.dstore.items() if i.kind == "plan"]
        run_id = "rparked01"
        api.loop.dstore._update(item.item_id, 5.0, state="awaiting_answers", run_id=run_id)
        api.ctx.plans.ask_questions(
            plan["id"],
            plan["root_id"],
            [PlanQuestion.model_validate(q) for q in self.QUESTIONS],
            run_id=run_id,
            now=6.0,
            item_id=item.item_id,
        )
        current = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        return current, item.item_id, run_id

    def _answer(self, api: Api, plan: dict[str, Any], headers: dict[str, str], **body: Any) -> Any:
        return api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/answers", json=body, headers=headers
        )

    def test_the_questions_are_read_from_the_plan(self, api: Api) -> None:
        plan, _item, run_id = self._parked(api)
        root = plan["nodes"][0]
        generation = root["generation"]
        assert generation["run_id"] == f"run_{run_id}"
        assert generation["status"] == "awaiting_answers"
        assert generation["answers"] == {} and generation["answered_at"] is None
        fmt, who = generation["questions"]
        assert fmt == {
            "id": "fmt",
            "prompt": "Which formats?",
            "choices": [
                {"value": "csv", "label": "CSV", "description": "a spreadsheet"},
                {"value": "pdf", "label": "PDF", "description": None},
            ],
            "allow_free_text": True,
        }
        assert who["allow_free_text"] is False
        assert [c["label"] for c in who["choices"]] == ["staff", "public"]
        (asked,) = _events(api, "plan.generation.questions")
        assert asked["run_id"] == f"run_{run_id}" and len(asked["questions"]) == 2

    def test_reading_alone_does_not_answer(self, api: Api) -> None:
        plan, _item, _run = self._parked(api)
        refused = self._answer(api, plan, api.bearer(READ), skip=True)
        assert refused.status_code == 403
        assert refused.json()["capability"] == "plans:create"

    def test_answers_resume_the_run(self, api: Api) -> None:
        plan, item_id, run_id = self._parked(api)
        headers = api.bearer(DRAFT)
        answered = self._answer(
            api,
            plan,
            headers,
            expected_revision=plan["revision"],
            answers={
                "fmt": {"value": "pdf", "text": "and keep the logo"},
                "who": {"value": "staff"},
            },
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["resumed"] is True and body["run_id"] == f"run_{run_id}"
        generation = body["plan"]["nodes"][0]["generation"]
        assert generation["status"] == "answered"
        assert generation["answers"] == {
            "fmt": {"value": "pdf", "text": "and keep the logo"},
            "who": {"value": "staff", "text": ""},
        }
        assert generation["answered_at"] is not None and generation["answered_by"]
        assert body["plan"]["revision"] == plan["revision"] + 1
        item = api.loop.dstore.get(item_id)
        assert item is not None and item.state == "queued" and item.run_id == run_id
        (event,) = _events(api, "plan.generation.answered")
        assert event["skipped"] is False and event["answers"]["who"] == {"value": "staff"}
        # Once is enough: the questions are settled.
        again = self._answer(api, body["plan"], headers, skip=True)
        assert again.status_code == 409 and again.json()["code"] == "already_answered"

    def test_a_skip_resumes_the_run_with_no_answers(self, api: Api) -> None:
        plan, item_id, _run = self._parked(api)
        skipped = self._answer(api, plan, api.bearer(DRAFT), skip=True)
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["plan"]["nodes"][0]["generation"]["status"] == "skipped"
        item = api.loop.dstore.get(item_id)
        assert item is not None and item.state == "queued"

    def test_answers_are_held_to_the_questions(self, api: Api) -> None:
        plan, item_id, _run = self._parked(api)
        headers = api.bearer(DRAFT)
        for body, detail in (
            ({"answers": {"nope": {"value": "csv"}}}, "no question 'nope'"),
            ({"answers": {"fmt": {"value": "xml"}}}, "'xml' is not a choice"),
            ({"answers": {"who": {"text": "everyone"}}}, "takes one of its choices"),
            ({"answers": {"fmt": {"value": "csv"}}, "skip": True}, "not both"),
            ({}, "answer at least one question"),
        ):
            refused = self._answer(api, plan, headers, **body)
            assert refused.status_code == 422, (body, refused.text)
            assert detail in refused.json()["detail"], refused.json()
        stale = self._answer(api, plan, headers, expected_revision=plan["revision"] - 1, skip=True)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        item = api.loop.dstore.get(item_id)
        assert item is not None and item.state == "awaiting_answers", "nothing was resumed"

    def test_nothing_waiting_is_refused_by_name(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        refused = self._answer(api, plan, headers, skip=True)
        assert refused.status_code == 409 and refused.json()["code"] == "no_questions"
        assert plan["nodes"][0]["generation"] is None

    def test_questions_whose_run_stopped_waiting_are_refused(self, api: Api) -> None:
        plan, item_id, _run = self._parked(api)
        api.loop.dstore._update(item_id, 7.0, state="failed")
        refused = self._answer(api, plan, api.bearer(DRAFT), skip=True)
        assert refused.status_code == 409 and refused.json()["code"] == "not_awaiting_answers"


class TestListing:
    def test_the_list_is_paged_most_recent_first(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        for index in range(3):
            api.harness.clock.t += 1
            _create(api, headers, title=f"Plan {index}")

        first = api.client.get("/v1/plans?limit=2", headers=headers)
        assert first.status_code == 200, first.text
        body = first.json()
        assert [p["title"] for p in body["data"]] == ["Plan 2", "Plan 1"]
        assert body["has_more"] and body["next_cursor"]

        second = api.client.get(f"/v1/plans?limit=2&cursor={body['next_cursor']}", headers=headers)
        assert second.status_code == 200, second.text
        rest = second.json()
        assert [p["title"] for p in rest["data"]] == ["Plan 0"]
        assert not rest["has_more"] and rest["next_cursor"] is None

        everything = api.client.get("/v1/plans", headers=headers).json()
        assert len(everything["data"]) == 3 and not everything["has_more"]

    def test_a_cursor_from_other_filters_is_refused(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        for index in range(2):
            api.harness.clock.t += 1
            _create(api, headers, title=f"Plan {index}")
        cursor = api.client.get("/v1/plans?limit=1", headers=headers).json()["next_cursor"]
        refused = api.client.get(f"/v1/plans?limit=1&level=epic&cursor={cursor}", headers=headers)
        assert refused.status_code == 400 and refused.json()["code"] == "invalid_cursor"
        garbage = api.client.get("/v1/plans?cursor=not-a-cursor", headers=headers)
        assert garbage.status_code == 400 and garbage.json()["code"] == "invalid_cursor"
