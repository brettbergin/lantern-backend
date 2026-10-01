"""A published plan follows the forge over ``/v1/plans`` (#2342).

``GET /v1/plans/{id}`` reconciles a published plan whose last reading is
older than ``[planning] reconcile_interval_s`` and never fails when the
forge cannot be read; ``POST .../sync`` (``plans:create``) reconciles now;
``POST .../drift/ack`` (``plans:create``) marks the forge's changes seen.
Nothing here writes to the forge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lantern.errors import GithubOpsError
from tests.api.conftest import Api, build
from tests.api.test_plans_publish import (
    DRAFT,
    PUBLISH,
    _epic_with_tasks,
    _events,
    _forge,
    _node,
    _publish,
)
from tests.fakes.fake_github import FakeGithub

READ: frozenset[Any] = frozenset({"runs:read"})


def _published(api: Api) -> tuple[FakeGithub, dict[str, Any], dict[str, str]]:
    fake = _forge(api)
    headers = api.bearer(PUBLISH)
    plan = _epic_with_tasks(api, headers)
    published = _publish(api, headers, plan)
    assert published.status_code == 200, published.text
    return fake, published.json()["plan"], headers


def _number(plan: dict[str, Any], title: str) -> int:
    return int(_node(plan, title)["forge"]["number"])


def _sync(api: Api, plan: dict[str, Any], headers: dict[str, str]) -> Any:
    return api.client.post(f"/v1/plans/{plan['id']}/sync", headers=headers)


def _writes(fake: FakeGithub, since: int) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in fake.raw_calls[since:] if m != "GET"]


class TestSync:
    def test_reading_alone_cannot_sync_and_drafting_can(self, api: Api) -> None:
        _, plan, _ = _published(api)
        refused = _sync(api, plan, api.bearer(READ))
        assert refused.status_code == 403, refused.text
        allowed = _sync(api, plan, api.bearer(DRAFT))
        assert allowed.status_code == 200, allowed.text

    def test_a_forge_edit_is_folded_in_reported_and_never_written_back(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        a = _number(plan, "A")
        fake.person_edits("o/r", a, title="A, as the team calls it", state="closed")
        calls = len(fake.raw_calls)
        synced = _sync(api, plan, headers)
        assert synced.status_code == 200, synced.text
        body = synced.json()
        node = _node(body, "A, as the team calls it")
        assert node["forge"]["state"] == "closed"
        assert [(d["change"], d["before"], d["after"]) for d in node["drift"]] == [
            ("title", {"title": "A"}, {"title": "A, as the team calls it"}),
            ("state", {"state": "open"}, {"state": "closed"}),
        ]
        assert body["drift"] == 1 and body["reconciled_at"] and body["reconcile_error"] is None
        assert body["revision"] == plan["revision"] + 1
        assert [e["change"] for e in _events(api, "plan.drift")] == ["title", "state"]
        assert _writes(fake, calls) == []

    def test_a_child_added_on_the_forge_is_adopted(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        number = fake.person_files("o/r", "Written on the forge", "Do this too.")
        fake.person_links("o/r", _number(plan, "An epic"), "o/r", number)
        body = _sync(api, plan, headers).json()
        adopted = _node(body, "Written on the forge")
        assert adopted["origin"] == "forge" and adopted["state"] == "published"
        assert adopted["level"] == "task" and adopted["goal"] == "Do this too."
        assert adopted["parent_id"] == plan["root_id"]
        assert [d["change"] for d in adopted["drift"]] == ["adopted"]

    def test_no_forge_connection_is_a_503_with_the_reason(self, api: Api) -> None:
        _, plan, headers = _published(api)
        api.loop.github = None
        refused = _sync(api, plan, headers)
        assert refused.status_code == 503, refused.text
        assert refused.json()["code"] == "source_unavailable"

    def test_an_archived_plan_is_not_synced(self, api: Api) -> None:
        _, plan, headers = _published(api)
        archived = api.client.delete(
            f"/v1/plans/{plan['id']}",
            params={"expected_revision": plan["revision"]},
            headers=headers,
        )
        assert archived.json()["outcome"] == "archived"
        refused = _sync(api, plan, headers)
        assert refused.status_code == 409 and refused.json()["code"] == "plan_archived"


class TestReadingReconciles:
    def test_a_stale_plan_is_reconciled_on_open_and_a_fresh_one_is_not(self, api: Api) -> None:
        fake, plan, _ = _published(api)
        reader = api.bearer(READ)
        fake.person_edits("o/r", _number(plan, "B"), title="B, renamed")
        # Published a moment ago and never read: due.
        first = api.client.get(f"/v1/plans/{plan['id']}", headers=reader).json()
        assert _node(first, "B, renamed")["drift"][0]["change"] == "title"
        fake.person_edits("o/r", _number(plan, "B"), title="B, again")
        api.clock.t += 60
        fresh = api.client.get(f"/v1/plans/{plan['id']}", headers=reader).json()
        assert [n["title"] for n in fresh["nodes"] if n["level"] == "task"] == ["A", "B, renamed"]
        api.clock.t += 120
        stale = api.client.get(f"/v1/plans/{plan['id']}", headers=reader).json()
        assert _node(stale, "B, again")["drift"][0]["before"] == {"title": "B"}

    def test_a_forge_that_is_down_never_fails_the_read(self, api: Api) -> None:
        fake, plan, _ = _published(api)
        fake.fail_always["issue_read"] = GithubOpsError("service unavailable", http_status=503)
        read = api.client.get(f"/v1/plans/{plan['id']}", headers=api.bearer(READ))
        assert read.status_code == 200, read.text
        body = read.json()
        assert "service unavailable" in str(body["reconcile_error"])
        assert body["revision"] == plan["revision"]

    def test_no_forge_connection_serves_the_stored_plan_with_the_reason(self, api: Api) -> None:
        _, plan, _ = _published(api)
        api.loop.github = None
        read = api.client.get(f"/v1/plans/{plan['id']}", headers=api.bearer(READ))
        assert read.status_code == 200, read.text
        assert read.json()["reconcile_error"] == "the daemon has no forge connection"

    def test_a_forge_connection_nothing_has_booted_is_not_booted_by_a_read(self, api: Api) -> None:
        fake, plan, _ = _published(api)
        api.loop.github.provisioned = False
        calls = len(fake.raw_calls)
        read = api.client.get(f"/v1/plans/{plan['id']}", headers=api.bearer(READ)).json()
        assert "sync" in str(read["reconcile_error"])
        assert len(fake.raw_calls) == calls

    def test_an_interval_of_zero_reads_only_on_sync(self, tmp_path: Path) -> None:
        built = build(tmp_path, config={"planning": {"reconcile_interval_s": 0}})
        with built.client:
            fake, plan, headers = _published(built)
            fake.person_edits("o/r", _number(plan, "A"), title="A2")
            read = built.client.get(f"/v1/plans/{plan['id']}", headers=headers).json()
            assert _node(read, "A")["drift"] == []
            synced = _sync(built, plan, headers).json()
            assert _node(synced, "A2")["drift"]
        built.ctx.close()


class TestDriftAck:
    def test_marking_drift_seen_clears_it_against_the_revision_read(self, api: Api) -> None:
        fake, plan, headers = _published(api)
        fake.person_edits("o/r", _number(plan, "A"), title="A2")
        fake.person_edits("o/r", _number(plan, "B"), title="B2")
        synced = _sync(api, plan, headers).json()
        url = f"/v1/plans/{plan['id']}/drift/ack"
        refused = api.client.post(
            url, json={"expected_revision": synced["revision"]}, headers=api.bearer(READ)
        )
        assert refused.status_code == 403
        stale = api.client.post(url, json={"expected_revision": plan["revision"]}, headers=headers)
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        one = api.client.post(
            url,
            json={"expected_revision": synced["revision"], "node_ids": [_node(synced, "A2")["id"]]},
            headers=headers,
        ).json()
        assert _node(one, "A2")["drift"] == [] and _node(one, "B2")["drift"]
        assert one["drift"] == 1
        rest = api.client.post(url, json={"expected_revision": one["revision"]}, headers=headers)
        assert rest.status_code == 200 and rest.json()["drift"] == 0
        seen = [e for e in _events(api, "plan.node.changed") if e["change"] == "drift_seen"]
        assert len(seen) == 2
