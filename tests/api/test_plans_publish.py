"""Approving and publishing a plan level over ``/v1/plans`` (#2341).

``POST .../nodes/{id}/approve`` is a person's "this is right" on a node's
children (``plans:create``). ``POST .../nodes/{id}/publish`` writes the
approved children — and the node itself first when it is not on the forge
yet — as issues (``plans:publish``, ``Idempotency-Key`` required). A
repeated or interrupted publish resumes by marker and never duplicates an
issue; a forge that cannot hold a plan is refused with the reason.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy import select

from lantern.daemon.controls.operations import OperationSpec, reconcile_operations
from lantern.daemon.controls.principal import Capability
from lantern.db.api_models import ApiEventRow
from lantern.errors import GithubOpsError
from lantern.vcs.checklist import parse_checklist
from tests.api.conftest import Api, build
from tests.fakes.fake_github import FakeGithub
from tests.fakes.fake_gitlab import FakeGitlab

DRAFT: frozenset[Capability] = frozenset({"runs:read", "plans:create"})
PUBLISH: frozenset[Capability] = frozenset({"runs:read", "plans:create", "plans:publish"})
MANAGE: frozenset[Capability] = frozenset({"runs:read", "daemon:manage"})


class Box:
    """The daemon's forge sandbox, answered by a fake."""

    def __init__(self, ops: Any, kind: str = "github") -> None:
        self.ops_obj = ops
        self.kind = kind
        self.failures: list[str] = []

    def ops(self) -> Any:
        return self.ops_obj

    def call(self, fn: Any) -> Any:
        return fn(self.ops_obj)

    def note_failure(self, exc: BaseException) -> bool:
        self.failures.append(str(exc))
        return False


def _forge(api: Api, ops: Any | None = None, kind: str = "github") -> Any:
    fake = ops if ops is not None else FakeGithub()
    api.loop.github = Box(fake, kind)
    return fake


def _create(api: Api, headers: dict[str, str], **body: Any) -> dict[str, Any]:
    payload = {"level": "epic", "repository": "o/r", "title": "An epic", **body}
    response = api.client.post("/v1/plans", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    # Publish tests begin after the planner authored a root. Intake itself
    # is exercised in test_plan_input.py; these tests exercise forge writes.
    from lantern.api.routes.plans import plan_out

    plan = api.ctx.plans.get(response.json()["id"])
    generated = replace(plan.root, **plan.input, origin="planner")
    changed = api.ctx.plans.store.apply(
        plan.id, expected_revision=plan.revision, now=api.clock(), upsert=[generated]
    )
    return plan_out(changed).model_dump(mode="json")


def _add(api: Api, headers: dict[str, str], plan: dict[str, Any], **body: Any) -> dict[str, Any]:
    response = api.client.post(
        f"/v1/plans/{plan['id']}/nodes",
        json={"expected_revision": plan["revision"], "parent_id": plan["root_id"], **body},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _node(plan: dict[str, Any], title: str) -> dict[str, Any]:
    return next(n for n in plan["nodes"] if n["title"] == title)


def _approve(
    api: Api, headers: dict[str, str], plan: dict[str, Any], node_id: str | None = None, **body: Any
) -> Any:
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{node_id or plan['root_id']}/approve",
        json={"expected_revision": plan["revision"], **body},
        headers=headers,
    )


def _publish(
    api: Api,
    headers: dict[str, str],
    plan: dict[str, Any],
    key: str | None = "k1",
    node_id: str | None = None,
    revision: int | None = None,
) -> Any:
    extra = {} if key is None else {"Idempotency-Key": key}
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{node_id or plan['root_id']}/publish",
        json={"expected_revision": revision or plan["revision"]},
        headers={**headers, **extra},
    )


def _epic_with_tasks(api: Api, headers: dict[str, str], repo: str = "o/r") -> dict[str, Any]:
    """A lone epic with tasks A and B (B depends on A), both approved."""
    plan = _create(api, headers, repository=repo, goal="Ship it")
    plan = _add(api, headers, plan, title="A", kind="code", acceptance_criteria=["works"])
    plan = _add(api, headers, plan, title="B", kind="code", depends_on=[_node(plan, "A")["id"]])
    approved = _approve(api, headers, plan)
    assert approved.status_code == 200, approved.text
    return dict(approved.json())


def _events(api: Api, type_: str) -> list[dict[str, Any]]:
    with api.ctx.collaboration.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
        )
        return [json.loads(row.data_json) for row in rows]


class TestApprove:
    def test_marks_the_proposed_and_draft_children_approved(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _create(api, headers)
        for title in ("A", "B", "C"):
            plan = _add(api, headers, plan, title=title)
        a, b = _node(plan, "A"), _node(plan, "B")
        some = _approve(api, headers, plan, node_ids=[a["id"], b["id"]])
        assert some.status_code == 200, some.text
        states = {n["title"]: n["state"] for n in some.json()["nodes"]}
        assert states == {"An epic": "draft", "A": "approved", "B": "approved", "C": "draft"}
        rest = _approve(api, headers, some.json())
        assert rest.status_code == 200, rest.text
        assert {n["state"] for n in rest.json()["nodes"] if n["level"] == "task"} == {"approved"}

    def test_a_node_that_is_not_a_child_is_refused(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _add(api, headers, _create(api, headers), title="A")
        refused = _approve(api, headers, plan, node_ids=["node_elsewhere"])
        assert refused.status_code == 422 and "node_elsewhere" in refused.json()["detail"]

    def test_nothing_left_to_approve_is_said(self, api: Api) -> None:
        headers = api.bearer(DRAFT)
        plan = _add(api, headers, _create(api, headers), title="A")
        plan = _approve(api, headers, plan).json()
        again = _approve(api, headers, plan)
        assert again.status_code == 422 and "nothing to approve" in again.json()["detail"]

    def test_reading_alone_does_not_approve(self, api: Api) -> None:
        plan = _add(api, api.bearer(DRAFT), _create(api, api.bearer(DRAFT)), title="A")
        refused = _approve(api, api.bearer(frozenset({"runs:read"})), plan)
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:create"


class TestWhoMayPublish:
    def test_drafting_alone_does_not_publish(self, api: Api) -> None:
        _forge(api)
        plan = _epic_with_tasks(api, api.bearer(DRAFT))
        refused = _publish(api, api.bearer(DRAFT), plan)
        assert refused.status_code == 403, refused.text
        assert refused.json()["capability"] == "plans:publish"

    def test_an_idempotency_key_is_required(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        refused = _publish(api, headers, plan, key=None)
        assert refused.status_code == 422
        assert refused.json()["code"] == "idempotency_key_required"
        assert fake.issues_created == []


class TestPublishingOnGithub:
    def test_a_fresh_epic_and_its_tasks_become_linked_sub_issues(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        response = _publish(api, headers, plan)
        assert response.status_code == 200, response.text
        body = response.json()
        results = body["results"]
        assert [(r["outcome"], r["linked"]) for r in results] == [
            ("created", "none"),
            ("created", "native"),
            ("created", "native"),
        ]
        epic, a, b = (r["number"] for r in results)
        assert fake.sub_issues[("o/r", epic)] == [("o/r", a), ("o/r", b)]
        published = body["plan"]
        assert published["state"] == "published"
        assert {n["state"] for n in published["nodes"]} == {"published"}
        forge = dict(_node(published, "A")["forge"])
        assert str(forge.pop("version")).startswith("c1-")
        assert forge == {
            "number": a,
            "url": f"https://github.com/o/r/issues/{a}",
            "state": "open",
            "updated_at": None,
            "detached": None,
            "marker_missing": False,
            "checklist_error": None,
        }
        assert body["replayed"] is False and body["operation_id"]

    def test_the_trigger_and_workload_labels_are_never_applied(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _create(api, headers)
        plan = _add(api, headers, plan, title="W", kind="workload")
        plan = _approve(api, headers, plan).json()
        assert _publish(api, headers, plan).status_code == 200
        applied = {label for _, _, labels in fake.issues_created for label in labels}
        lifecycle = api.ctx.config.labels_for("o/r")
        assert applied == {"sbx:epic", "sbx:task"}
        assert lifecycle.trigger not in applied and lifecycle.workload not in applied

    def test_a_repeated_publish_creates_nothing_new(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        first = _publish(api, headers, plan).json()["plan"]
        again = _publish(api, headers, first, key="k2")
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "nothing_to_publish"
        assert len(fake.issues_created) == 3

    def test_a_replay_answers_the_same_and_writes_nothing(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        first = _publish(api, headers, plan)
        replay = _publish(api, headers, plan)
        assert replay.status_code == 200, replay.text
        assert replay.json()["results"] == first.json()["results"]
        assert replay.json()["operation_id"] == first.json()["operation_id"]
        assert replay.json()["replayed"] is True
        assert len(fake.issues_created) == 3
        other = _publish(api, headers, plan, revision=plan["revision"] + 1)
        assert other.status_code == 409 and other.json()["code"] == "idempotency_conflict"

    def test_an_interrupted_publish_resumes_without_duplicating(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        fake.fail_once["sub_issue_add"] = GithubOpsError("secondary rate limit", http_status=403)
        first = _publish(api, headers, plan)
        assert first.status_code == 200, first.text
        outcomes = [r["outcome"] for r in first.json()["results"]]
        assert outcomes == ["created", "failed", "failed"]
        after = first.json()["plan"]
        assert _node(after, "A")["state"] == "approved"
        resumed = _publish(api, headers, after, key="k2")
        assert resumed.status_code == 200, resumed.text
        assert [(r["outcome"], r["linked"]) for r in resumed.json()["results"]] == [
            ("found", "native"),
            ("created", "native"),
        ]
        assert [title for title, _, _ in fake.issues_created] == ["An epic", "A", "B"]
        published = _events(api, "plan.published")
        root = plan["root_id"]
        a, b = _node(plan, "A")["id"], _node(plan, "B")["id"]
        assert published == [
            {"plan_id": plan["id"], "node_id": root, "published": [root], "failed": [a, b]},
            {"plan_id": plan["id"], "node_id": root, "published": [a, b], "failed": []},
        ]

    def test_a_stale_revision_is_refused_before_the_forge(self, api: Api) -> None:
        fake = _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        stale = _publish(api, headers, plan, revision=plan["revision"] - 1)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        assert fake.issues_created == []


class TestRefusals:
    def test_a_forge_that_cannot_hold_a_plan_is_refused_with_the_reason(self, api: Api) -> None:
        fake = _forge(api)
        manage = api.bearer(MANAGE)
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "acme/gadgets", "forge": "gitea"},
            headers=manage,
        )
        assert created.status_code == 201, created.text
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        # A plan whose repository has since moved to Gitea.
        current = api.ctx.plans.get(plan["id"])
        api.ctx.plans.store.apply(
            plan["id"],
            expected_revision=current.revision,
            now=api.clock(),
            upsert=[replace(n, repository="acme/gadgets") for n in current.nodes],
        )
        plan = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        refused = _publish(api, headers, plan)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "planning_unsupported"
        assert "Gitea is not supported" in refused.json()["detail"]
        assert fake.issues_created == []

    def test_a_disabled_repository_is_refused(self, api: Api) -> None:
        _forge(api)
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "o/off", "forge": "github", "enabled": False},
            headers=api.bearer(MANAGE),
        )
        assert created.status_code == 201, created.text
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers, repo="o/off")
        refused = _publish(api, headers, plan)
        assert refused.status_code == 409 and refused.json()["code"] == "repository_disabled"

    def test_more_children_than_the_cap_is_refused(self, tmp_path: Path) -> None:
        """The cap holds when a child is drafted, so a person hears about
        it then — and publish checks it again, for a cap lowered since."""
        built = build(tmp_path, config={"planning": {"max_tasks_per_epic": 2}})
        with built.client:
            _forge(built)
            headers = built.bearer(PUBLISH)
            plan = _epic_with_tasks(built, headers)
            refused = built.client.post(
                f"/v1/plans/{plan['id']}/nodes",
                json={
                    "expected_revision": plan["revision"],
                    "parent_id": plan["root_id"],
                    "title": "C",
                    "kind": "code",
                },
                headers=headers,
            )
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "level_full"

            lowered = built.harness.loop.config.model_copy(deep=True)
            lowered.planning.max_tasks_per_epic = 1
            built.harness.loop.config = lowered
            refused = _publish(built, headers, plan)
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "too_many_children"
            assert refused.json()["cap"] == 1
        built.ctx.close()

    def test_a_dependency_that_is_not_being_published_is_named(self, api: Api) -> None:
        _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _create(api, headers)
        plan = _add(api, headers, plan, title="A")
        plan = _add(api, headers, plan, title="B", depends_on=[_node(plan, "A")["id"]])
        plan = _approve(api, headers, plan, node_ids=[_node(plan, "B")["id"]]).json()
        refused = _publish(api, headers, plan)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "dependency_unpublished"
        assert "A" in refused.json()["detail"]

    def test_a_repository_on_another_forge_than_the_daemons_is_refused(self, api: Api) -> None:
        _forge(api, FakeGitlab(), kind="gitlab")
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        refused = _publish(api, headers, plan)
        assert refused.status_code == 409 and refused.json()["code"] == "forge_mismatch"
        assert "GitHub" in refused.json()["detail"] and "GitLab" in refused.json()["detail"]

    def test_no_forge_connection_is_unavailable(self, api: Api) -> None:
        api.loop.github = None
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        refused = _publish(api, headers, plan)
        assert refused.status_code == 503 and refused.json()["code"] == "source_unavailable"

    def test_a_refusal_replays_as_the_same_refusal(self, api: Api) -> None:
        api.loop.github = None
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers)
        first = _publish(api, headers, plan)
        _forge(api)
        again = _publish(api, headers, plan)
        assert again.status_code == first.status_code == 503
        assert again.json()["code"] == "source_unavailable"


class TestPublishingOnGitlab:
    def test_the_parent_carries_the_checklist_and_nothing_else_of_it_changes(
        self, api: Api
    ) -> None:
        created = api.client.post(
            "/v1/repositories",
            json={"repository": "acme/widgets", "forge": "gitlab"},
            headers=api.bearer(MANAGE),
        )
        assert created.status_code == 201, created.text
        fake = _forge(api, FakeGitlab(), kind="gitlab")
        headers = api.bearer(PUBLISH)
        plan = _epic_with_tasks(api, headers, repo="acme/widgets")
        response = _publish(api, headers, plan)
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert [r["linked"] for r in results] == ["none", "checklist", "checklist"]
        epic, a, b = (r["number"] for r in results)
        rendered = fake.issues_created[0][1].rstrip("\n")
        description = str(fake.issues[epic]["description"])
        assert description.startswith(rendered)
        assert [e.ref for e in parse_checklist(description)] == [
            f"acme/widgets#{a}",
            f"acme/widgets#{b}",
        ]
        assert not any("sub_issue" in path for _, path, _ in fake.raw_calls)


class TestAnInterruptedOperation:
    def test_a_publish_the_daemon_died_during_is_settled_failed_not_left_running(
        self, api: Api
    ) -> None:
        from lantern.daemon.controls.principal import Principal

        store = api.loop.operations
        op, _ = store.accept(
            OperationSpec(
                action="plan.publish",
                target_kind="plan",
                target_key="plan_x",
                principal=Principal.trusted("tester", "test"),
                idempotency=("scope", "key"),
            ),
            api.clock(),
        )
        store.claim(op.id, "an-earlier-generation", api.clock())
        (settled,) = [
            o
            for o in reconcile_operations(api.loop, generation="now", now=api.clock())
            if o.id == op.id
        ]
        assert settled.state == "failed"
        assert settled.error_code == "interrupted_before_effect"
        assert "resume" in str(settled.error_detail)
