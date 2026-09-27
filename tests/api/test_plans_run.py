"""Running a published epic over ``/v1/plans`` (#2347).

``POST .../nodes/{epic}/run`` (``plans:publish``, ``Idempotency-Key``
required) starts a daemon-owned epic run: the tasks whose dependencies are
closed are admitted at once through the issue admission ``POST /v1/items``
uses, with no queueing label and ``parent_item_id`` naming the run.
``GET .../run`` reads it back, each task with the item and run it became.
The forge is the same fake publishing wrote the issues to.
"""

from __future__ import annotations

from typing import Any

from sbxloop.daemon.controls.operations import OperationSpec, reconcile_operations
from sbxloop.daemon.controls.principal import Capability
from sbxloop.daemon.sources import GitHubIssueSource
from tests.api.conftest import Api
from tests.api.test_plans_publish import (
    DRAFT,
    PUBLISH,
    _add,
    _approve,
    _create,
    _epic_with_tasks,
    _events,
    _forge,
    _node,
    _publish,
)
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_daemon_sources import LABELS

READ: frozenset[Capability] = frozenset({"runs:read"})


def _published(api: Api) -> tuple[FakeGithub, dict[str, str], dict[str, Any]]:
    """A published lone epic with tasks A and B (B depends on A), and the
    daemon's issue source over the same fake forge."""
    fake = _forge(api)
    api.loop.source = GitHubIssueSource(lambda: fake, "o/r", LABELS, host="db")  # type: ignore[arg-type]
    headers = api.bearer(PUBLISH)
    plan = _epic_with_tasks(api, headers)
    published = _publish(api, headers, plan)
    assert published.status_code == 200, published.text
    return fake, headers, dict(published.json()["plan"])


def _run(
    api: Api, headers: dict[str, str], plan: dict[str, Any], key: str | None = "r1", **body: Any
) -> Any:
    extra = {} if key is None else {"Idempotency-Key": key}
    return api.client.post(
        f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/run",
        json={"expected_revision": plan["revision"], **body},
        headers={**headers, **extra},
    )


class TestStartingARun:
    def test_ready_tasks_are_admitted_without_the_trigger_label(self, api: Api) -> None:
        fake, headers, plan = _published(api)
        before = len(fake.raw_calls)
        response = _run(api, headers, plan)
        assert response.status_code == 201, response.text
        assert response.headers["Location"] == f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/run"
        body = response.json()
        assert body["state"] == "running" and body["id"].startswith("erun_")
        assert body["started_by_display"] and body["replayed"] is False
        tasks = {t["title"]: t for t in body["tasks"]}
        assert tasks["A"]["state"] == "queued" and tasks["A"]["item_id"]
        assert tasks["B"]["state"] == "waiting" and tasks["B"]["item_id"] is None
        assert tasks["B"]["depends_on"] == [_node(plan, "A")["id"]]
        assert tasks["A"]["forge"]["number"] == _node(plan, "A")["forge"]["number"]
        (item,) = api.loop.dstore.items()
        assert item.item_id == tasks["A"]["item_id"]
        assert item.parent_item_id == body["id"] and item.kind == "code"
        # Admission read the issue and wrote nothing to it: no label.
        writes = [(m, p) for m, p, _ in fake.raw_calls[before:] if m != "GET"]
        assert writes == []
        started = _events(api, "plan.run.started")
        assert started == [
            {"plan_id": plan["id"], "node_id": plan["root_id"], "epic_run_id": body["id"]}
        ]
        (admitted,) = _events(api, "plan.run.task_admitted")
        assert admitted["task_node_id"] == _node(plan, "A")["id"]
        assert admitted["item_id"] == item.item_id

    def test_the_run_reads_back_with_each_tasks_item(self, api: Api) -> None:
        _, headers, plan = _published(api)
        started = _run(api, headers, plan).json()
        read = api.client.get(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/run", headers=api.bearer(READ)
        )
        assert read.status_code == 200, read.text
        assert read.json()["id"] == started["id"]
        assert [t["item_id"] for t in read.json()["tasks"]] == [
            t["item_id"] for t in started["tasks"]
        ]
        unrun = api.client.get(
            f"/v1/plans/{plan['id']}/nodes/{_node(plan, 'A')['id']}/run",
            headers=api.bearer(READ),
        )
        assert unrun.status_code == 404

    def test_a_replay_answers_the_same_run_and_admits_nothing_new(self, api: Api) -> None:
        _, headers, plan = _published(api)
        first = _run(api, headers, plan)
        replay = _run(api, headers, plan)
        assert replay.status_code == 200, replay.text
        assert replay.json()["id"] == first.json()["id"]
        assert replay.json()["replayed"] is True
        assert replay.json()["operation_id"] == first.json()["operation_id"]
        assert len(api.loop.dstore.items()) == 1
        other = _run(api, headers, plan, expected_revision=plan["revision"] + 1)
        assert other.status_code == 409 and other.json()["code"] == "idempotency_conflict"
        again = _run(api, headers, plan, key="r2")
        assert again.status_code == 409, again.text
        assert again.json()["code"] == "already_running"
        assert again.json()["epic_run_id"] == first.json()["id"]

    def test_an_idempotency_key_is_required(self, api: Api) -> None:
        _, headers, plan = _published(api)
        refused = _run(api, headers, plan, key=None)
        assert refused.status_code == 422
        assert refused.json()["code"] == "idempotency_key_required"


class TestRefusals:
    def test_drafting_alone_does_not_run_an_epic(self, api: Api) -> None:
        _, _, plan = _published(api)
        refused = _run(api, api.bearer(DRAFT), plan)
        assert refused.status_code == 403, refused.text
        assert refused.json()["capability"] == "plans:publish"
        assert api.loop.dstore.items() == []

    def test_an_unpublished_epic_is_refused(self, api: Api) -> None:
        _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _add(api, headers, _create(api, headers), title="A", kind="code")
        refused = _run(api, headers, plan)
        assert refused.status_code == 409 and refused.json()["code"] == "epic_unpublished"

    def test_an_epic_with_no_task_on_the_forge_is_refused(self, api: Api) -> None:
        _forge(api)
        headers = api.bearer(PUBLISH)
        plan = _add(api, headers, _create(api, headers), title="A", kind="code")
        plan = _approve(api, headers, plan).json()
        # Publish the epic alone: A is left out by un-approving it.
        plan = api.client.patch(
            f"/v1/plans/{plan['id']}/nodes/{_node(plan, 'A')['id']}",
            json={"expected_revision": plan["revision"], "goal": "later"},
            headers=headers,
        ).json()
        assert _publish(api, headers, plan).status_code == 200
        plan = api.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
        refused = _run(api, headers, plan)
        assert refused.status_code == 409 and refused.json()["code"] == "nothing_to_run"

    def test_a_refusal_replays_as_the_same_refusal(self, api: Api) -> None:
        _, headers, plan = _published(api)
        stale = _run(api, headers, plan, expected_revision=plan["revision"] + 5)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        again = _run(api, headers, plan, expected_revision=plan["revision"] + 5)
        assert again.status_code == 409 and again.json()["code"] == "stale_revision"
        assert again.json()["operation_id"] == stale.json()["operation_id"]


class TestAdvertised:
    def test_planning_run_is_a_feature(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer(READ)).json()["features"]
        assert "planning" in features and "planning.run" in features

    def test_a_run_the_daemon_died_during_is_settled_from_the_record(self, api: Api) -> None:
        from sbxloop.daemon.controls.principal import Principal

        store = api.loop.operations
        op, _ = store.accept(
            OperationSpec(
                action="plan.run",
                target_kind="plan",
                target_key="plan_x",
                principal=Principal.trusted("tester", "test"),
                request={"plan_id": "plan_x", "node_id": "node_x"},
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
