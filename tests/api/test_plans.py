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

from sbxloop.daemon.controls.principal import ROLE_CAPABILITIES, Capability
from sbxloop.db.api_models import ApiEventRow
from sbxloop.plans.model import PlanNode
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
        assert read.status_code == 200 and read.json()["nodes"][0]["title"] == "Plan the work"

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


class TestTheTree:
    def test_an_initiative_breaks_into_epics_and_an_epic_into_tasks(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers, goal="ship planning", acceptance_criteria=["it works"])
        root = plan["nodes"][0]
        assert root["level"] == "initiative" and root["state"] == "draft"
        assert root["origin"] == "person" and root["acceptance_criteria"] == ["it works"]
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
        assert [n["title"] for n in plan["nodes"]] == ["Plan the work", "API", "Store", "Routes"]
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
            api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()["nodes"][0]["goal"]
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
        from sbxloop.plans.model import ForgeRef

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
        built.ctx.close()
