"""Re-planning a published node as an approved diff over ``/v1/plans`` (#2346).

A breakdown of a node on the forge with children there queues a re-plan:
its run proposes a diff — ``add``, ``modify``, ``suggest_close`` — that
waits on the node and writes nothing. ``POST .../replan/approve``
(``plans:publish``, ``Idempotency-Key`` required) applies entries of it
through the publish path: an addition is published with its marker, level
label and sub-issue link and never filed twice; a change rewrites only the
sections it changes and is refused when the child moved on the forge; a
close closes the issue as not planned with a comment. ``POST
.../replan/discard`` (``plans:create``) drops entries.
"""

from __future__ import annotations

from typing import Any

import pytest

from lantern.engine.planning import PlanReplan
from lantern.plans.render import marked, marker, parse_sections
from tests.api.conftest import Api
from tests.api.test_plans_publish import DRAFT, _events, _node
from tests.api.test_plans_reconcile import _number, _published, _writes

READ: frozenset[Any] = frozenset({"runs:read"})


def _task(title: str, **over: Any) -> dict[str, Any]:
    return {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "goal": f"{title} is done",
        "acceptance_criteria": [f"{title} works"],
        "kind": "code",
        "verify_commands": ["make test"],
    } | over


def _propose(api: Api, plan: dict[str, Any], **entries: list[dict[str, Any]]) -> dict[str, Any]:
    """What a re-plan run delivers: the diff, kept on the node."""
    replan = PlanReplan.model_validate({"add": [], "modify": [], "suggest_close": []} | entries)
    api.ctx.plans.deliver_replan(
        plan["id"], plan["root_id"], replan, run_id="r1replan", now=api.clock()
    )
    return _read(api, plan)


def _read(api: Api, plan: dict[str, Any]) -> dict[str, Any]:
    body = api.client.get(f"/v1/plans/{plan['id']}", headers=api.bearer(READ)).json()
    return dict(body)


def _entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    replan = _node(plan, "An epic")["replan"]
    return [] if replan is None else list(replan["entries"])


def _approve(
    api: Api,
    headers: dict[str, str],
    plan: dict[str, Any],
    key: str | None = "k1",
    **body: Any,
) -> Any:
    extra = {} if key is None else {"Idempotency-Key": key}
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/replan/approve",
        json={"expected_revision": plan["revision"], **body},
        headers={**headers, **extra},
    )


def _discard(api: Api, headers: dict[str, str], plan: dict[str, Any], **body: Any) -> Any:
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/replan/discard",
        json={"expected_revision": plan["revision"], **body},
        headers=headers,
    )


class TestTheRun:
    def test_a_breakdown_of_a_published_node_with_children_queues_a_replan(self, api: Api) -> None:
        _, plan, headers = _published(api)
        plan = _read(api, plan)
        accepted = api.client.post(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/breakdown",
            json={"expected_revision": plan["revision"], "note": "split the slow one"},
            headers=headers,
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["item"]["title"] == "Re-plan the tasks of “An epic”"
        # The brief it will be asked: every current child, by id.
        brief = api.ctx.plans.brief(plan["id"], plan["root_id"])
        assert brief.mode == "replan"
        assert [c.title for c in brief.current] == ["A", "B"]
        assert all(c.changeable and c.owned and c.issue.startswith("o/r#") for c in brief.current)
        assert brief.room == 12 - 2


class TestTheDiffWaits:
    def test_the_diff_is_on_the_node_and_nothing_is_written(self, api: Api) -> None:
        fake, plan, _ = _published(api)
        calls = len(fake.raw_calls)
        a = _node(plan, "A")
        plan = _propose(
            api,
            plan,
            add=[_task("C", rationale="a step is missing")],
            modify=[{"target": a["id"], "goal": "A, sharper", "rationale": "r"}],
            suggest_close=[{"target": _node(plan, "B")["id"], "rationale": "not needed"}],
        )
        entries = _entries(plan)
        assert [e["action"] for e in entries] == ["add", "modify", "suggest_close"]
        add, modify, _close = entries
        assert add["sections"]["title"] == "C" and add["rationale"] == "a step is missing"
        assert add["node_id"] not in {n["id"] for n in plan["nodes"]}, "not a child yet"
        assert modify["node_id"] == a["id"]
        assert modify["sections"] == {"goal": "A, sharper"}
        assert modify["before"] == {"goal": ""}
        replan = _node(plan, "An epic")["replan"]
        assert replan["run_id"] == "run_r1replan" and replan["proposed_at"]
        (proposed,) = _events(api, "plan.generation.proposed")
        assert proposed["kind"] == "replan"
        assert (proposed["add"], proposed["modify"], proposed["suggest_close"]) == (1, 1, 1)
        assert proposed["skipped"] == 0 and proposed["count"] == 3
        assert _writes(fake, calls) == []
        assert [n["title"] for n in plan["nodes"]] == ["An epic", "A", "B"]

    def test_an_addition_that_repeats_a_child_is_never_kept(self, api: Api) -> None:
        _, plan, _ = _published(api)
        plan = _propose(api, plan, add=[_task(" a "), _task("C")])
        assert [e["sections"]["title"] for e in _entries(plan)] == ["C"]
        (proposed,) = _events(api, "plan.generation.proposed")
        assert proposed["skipped"] == 1 and proposed["add"] == 1

    def test_a_new_diff_replaces_the_one_waiting(self, api: Api) -> None:
        _, plan, _ = _published(api)
        _propose(api, plan, add=[_task("C")])
        plan = _propose(api, plan, add=[_task("D")])
        assert [e["sections"]["title"] for e in _entries(plan)] == ["D"]


class TestApprovingAnAddition:
    def test_it_is_published_through_the_publish_path(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        plan = _propose(api, plan, add=[_task("C", depends_on=[_node(plan, "A")["id"]])])
        (entry,) = _entries(plan)
        before = len(fake.issues_created)

        response = _approve(api, headers, plan)

        assert response.status_code == 200, response.text
        body = response.json()
        (result,) = body["results"]
        assert result["entry_id"] == entry["id"] and result["action"] == "add"
        assert result["outcome"] == "created" and result["node_id"] == entry["node_id"]
        # One issue, with its marker and level label, linked as a sub-issue.
        assert len(fake.issues_created) == before + 1
        title, issue_body, labels = fake.issues_created[-1]
        assert title == "C" and labels == ["sbx:task"]
        assert marked(issue_body, plan["id"], entry["node_id"])
        assert f"#{_number(plan, 'A')}" in parse_sections(issue_body)["depends_on"]
        epic = _number(plan, "An epic")
        assert ("o/r", result["number"]) in fake.sub_issues[("o/r", epic)]
        # It is a child of the plan now, and the diff is spent.
        applied = body["plan"]
        child = _node(applied, "C")
        assert child["id"] == entry["node_id"] and child["state"] == "published"
        assert child["origin"] == "planner" and child["forge"]["number"] == result["number"]
        assert _node(applied, "An epic")["replan"] is None
        (published,) = _events(api, "plan.published")[-1:]
        assert published["replan"] is True and published["published"] == [entry["node_id"]]

    def test_an_issue_an_earlier_attempt_filed_is_found_by_its_marker(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        plan = _propose(api, plan, add=[_task("C")])
        (entry,) = _entries(plan)
        # An earlier approval filed the issue and died before recording it.
        fake.issue_create(
            "o/r", "C", f"## Goal\n\nx\n\n{marker(plan['id'], entry['node_id'])}\n", ["sbx:task"]
        )
        filed = len(fake.issues_created)
        response = _approve(api, headers, plan)
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert result["outcome"] == "found"
        assert len(fake.issues_created) == filed, "never filed twice"

    def test_a_child_with_the_title_filed_meanwhile_is_not_duplicated(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        plan = _propose(api, plan, add=[_task("C")])
        # A person files the same child on the forge before anyone approves.
        number = fake.person_files("o/r", "C", "Do C.")
        fake.person_links("o/r", _number(plan, "An epic"), "o/r", number)
        filed = len(fake.issues_created)
        response = _approve(api, headers, plan)
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert result["outcome"] == "failed" and "exists already" in result["error"]
        assert len(fake.issues_created) == filed
        after = response.json()["plan"]
        assert [n["title"] for n in after["nodes"]].count("C") == 1
        assert _entries(after)[0]["error"] == result["error"], "the entry waits with its error"


class TestApprovingAChange:
    def test_only_the_sections_it_changes_are_written(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        a = _number(plan, "A")
        issue = next(i for i in fake.existing_issues if i["number"] == a)
        fake.person_edits("o/r", a, body=str(issue["body"]) + "\nNotes a person keeps here.\n")
        plan = _propose(
            api,
            plan,
            modify=[
                {
                    "target": _node(plan, "A")["id"],
                    "acceptance_criteria": ["works", "and fast"],
                    "rationale": "r",
                }
            ],
        )
        response = _approve(api, headers, plan)
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert (result["outcome"], result["number"]) == ("updated", a)
        ((number, fields),) = fake.issues_updated
        assert number == a and set(fields) == {"body"}
        assert parse_sections(fields["body"])["acceptance_criteria"] == ("works", "and fast")
        assert "Notes a person keeps here." in fields["body"]
        assert marked(fields["body"], plan["id"], _node(plan, "A")["id"])
        node = _node(response.json()["plan"], "A")
        assert node["acceptance_criteria"] == ["works", "and fast"] and node["drift"] == []

    def test_a_child_changed_on_the_forge_since_the_diff_is_refused(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        a = _node(plan, "A")
        plan = _propose(
            api, plan, modify=[{"target": a["id"], "acceptance_criteria": ["x"], "rationale": "r"}]
        )
        # A person rewrites the same section on the forge after the diff.
        issue = next(i for i in fake.existing_issues if i["number"] == a["forge"]["number"])
        fake.person_edits(
            "o/r",
            a["forge"]["number"],
            body=str(issue["body"]).replace("- [ ] works", "- [ ] works, as a person put it"),
        )
        response = _approve(api, headers, plan)
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert result["outcome"] == "failed"
        assert "changed since the re-plan was proposed (acceptance_criteria)" in result["error"]
        assert fake.issues_updated == [], "nothing was written over the person's edit"
        node = _node(response.json()["plan"], "A")
        assert node["acceptance_criteria"] == ["works, as a person put it"]

    def test_an_issue_changed_after_the_reading_is_refused_by_the_shared_write(
        self, api: Api, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The change goes through the direct edit's write, which reads the
        issue itself: an edit the reconcile did not see still refuses it."""
        from lantern.plans import service

        fake, plan, headers = _published(api)
        a = _node(plan, "A")
        plan = _propose(
            api, plan, modify=[{"target": a["id"], "goal": "A, sharper", "rationale": "r"}]
        )
        (entry,) = _entries(plan)
        assert entry["forge_version"] == a["forge"]["version"]
        monkeypatch.setattr(service, "reconcile_plan", lambda *args, **kwargs: None)
        fake.person_edits("o/r", a["forge"]["number"], title="A, as a person put it")
        response = _approve(api, headers, plan)
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert result["outcome"] == "failed"
        assert "changed on the forge since the re-plan was proposed" in result["error"]
        assert fake.issues_updated == []


class TestApprovingAClose:
    def test_the_issue_is_closed_as_not_planned_with_the_reason(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        b = _number(plan, "B")
        plan = _propose(
            api,
            plan,
            suggest_close=[{"target": _node(plan, "B")["id"], "rationale": "A covers it now."}],
        )
        response = _approve(api, headers, plan)
        assert response.status_code == 200, response.text
        (result,) = response.json()["results"]
        assert (result["outcome"], result["number"]) == ("closed", b)
        assert fake.issues_closed == [(b, "not_planned")]
        ((number, comment),) = fake.issue_answers
        assert number == b and "> A covers it now." in comment
        assert "Closed as not planned" in comment
        node = _node(response.json()["plan"], "B")
        assert node["forge"]["state"] == "closed" and node["state"] == "published"
        (published,) = _events(api, "plan.published")[-1:]
        assert published["closed"] == [node["id"]]


class TestDiscard:
    def test_discarding_drops_entries_and_writes_nothing(self, api: Api) -> None:
        fake, plan, _ = _published(api)
        plan = _propose(api, plan, add=[_task("C"), _task("D")])
        first, second = _entries(plan)
        calls = len(fake.raw_calls)
        some = _discard(api, api.bearer(DRAFT), plan, entry_ids=[first["id"]])
        assert some.status_code == 200, some.text
        assert [e["id"] for e in _entries(some.json())] == [second["id"]]
        rest = _discard(api, api.bearer(DRAFT), some.json())
        assert rest.status_code == 200 and _node(rest.json(), "An epic")["replan"] is None
        assert _writes(fake, calls) == []
        changes = [e["change"] for e in _events(api, "plan.node.changed")]
        assert changes.count("replan_discarded") == 2
        nothing = _discard(api, api.bearer(DRAFT), rest.json())
        assert nothing.status_code == 409 and nothing.json()["code"] == "no_replan"


class TestRefusals:
    def test_who_may(self, api: Api) -> None:
        _, plan, _ = _published(api)
        plan = _propose(api, plan, add=[_task("C")])
        refused = _approve(api, api.bearer(DRAFT), plan)
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:publish"
        refused = _discard(api, api.bearer(READ), plan)
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:create"

    def test_an_idempotency_key_is_required_and_a_replay_writes_nothing(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        plan = _propose(api, plan, add=[_task("C")])
        refused = _approve(api, headers, plan, key=None)
        assert refused.status_code == 422
        assert refused.json()["code"] == "idempotency_key_required"
        first = _approve(api, headers, plan, key="once")
        assert first.status_code == 200, first.text
        filed = len(fake.issues_created)
        again = _approve(api, headers, plan, key="once")
        assert again.status_code == 200, again.text
        assert again.json()["replayed"] is True
        assert again.json()["results"] == first.json()["results"]
        assert len(fake.issues_created) == filed

    def test_nothing_waiting_and_an_unknown_entry(self, api: Api) -> None:
        _, plan, headers = _published(api)
        plan = _read(api, plan)
        none = _approve(api, headers, plan)
        assert none.status_code == 409 and none.json()["code"] == "no_replan"
        plan = _propose(api, plan, add=[_task("C")])
        unknown = _approve(api, headers, plan, key="k2", entry_ids=["rpe_elsewhere"])
        assert unknown.status_code == 422 and "rpe_elsewhere" in unknown.json()["detail"]

    def test_an_addition_past_the_cap_is_refused_before_the_forge(self, api: Api) -> None:
        from lantern.config import PlanningConfig

        fake, plan, headers = _published(api)
        plan = _propose(api, plan, add=[_task("C")])
        tight = api.ctx.config.model_copy(update={"planning": PlanningConfig(max_tasks_per_epic=2)})
        api.ctx.config = tight
        api.loop.config = tight
        calls = len(fake.raw_calls)
        refused = _approve(api, headers, plan)
        assert refused.status_code == 409 and refused.json()["code"] == "too_many_children"
        assert fake.raw_calls[calls:] == []

    def test_no_forge_connection_is_a_503(self, api: Api) -> None:
        _, plan, headers = _published(api)
        plan = _propose(api, plan, add=[_task("C")])
        api.loop.github = None
        refused = _approve(api, headers, plan)
        assert refused.status_code == 503 and refused.json()["code"] == "source_unavailable"
