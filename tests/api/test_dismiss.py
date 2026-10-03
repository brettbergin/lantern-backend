"""Dismissing an alert: an acknowledgement everyone sees, recorded as an
operation, that takes nothing away from the work and ends by itself when
the work moves again."""

from __future__ import annotations

import json
from typing import Any

from tests.api.conftest import Api
from tests.api.test_control import run_public
from tests.api.test_external_work import channels, external_item
from tests.unit.test_daemon_loop import gh_item


def _blocked(api: Api, key: str = "1") -> dict[str, Any]:
    """An item whose run ended blocked — the alert a person is shown."""
    api.harness.source.items = [gh_item(key)]
    api.harness.outcomes = ["blocked"]
    assert api.loop.tick().outcome == "blocked"
    (item,) = api.client.get("/v1/items", headers=api.bearer()).json()["data"]
    assert item["state"] == "blocked" and item["dismissal"] is None
    return dict(item)


def _item(api: Api, item_id: str, headers: dict[str, str]) -> dict[str, Any]:
    response = api.client.get(f"/v1/items/{item_id}", headers=headers)
    assert response.status_code == 200, response.text
    return dict(response.json())


class TestAnItem:
    def test_the_alert_is_dismissed_for_everyone_and_the_work_keeps_its_controls(
        self, api: Api
    ) -> None:
        item = _blocked(api)
        assert item["available_actions"] == ["retry", "abandon", "dismiss"]
        headers = api.bearer()
        response = api.client.post(
            f"/v1/items/{item['id']}/dismiss", json={"reason": "known flake"}, headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["operation"]["action"] == "item.dismiss"
        assert body["operation"]["state"] == "succeeded"
        dismissal = body["item"]["dismissal"]
        assert dismissal["cause"] == "dismissed" and dismissal["reason"] == "known flake"
        assert dismissal["by"]["via"] == "api" and dismissal["at"].endswith("Z")
        assert dismissal["operation_id"] == body["operation"]["id"]
        # Nothing about the work moved: the same state at the same revision.
        assert body["item"]["state"] == "blocked"
        assert body["item"]["revision"] == item["revision"]
        assert body["item"]["available_actions"] == ["retry", "abandon", "undismiss"]
        # Another person reads the same dismissal, on the item and on its run.
        other = api.bearer(frozenset({"runs:read"}))
        assert _item(api, item["id"], other)["dismissal"] == dismissal
        run = api.client.get(f"/v1/runs/{item['run_id']}", headers=other).json()
        assert run["dismissal"] == dismissal
        # Work an item carries is dismissed through the item alone.
        assert not {"dismiss", "undismiss"} & set(run["available_actions"])

    def test_dismissing_twice_keeps_the_first_dismissal(self, api: Api) -> None:
        item = _blocked(api)
        headers = api.bearer()
        first = api.client.post(f"/v1/items/{item['id']}/dismiss", headers=headers).json()
        again = api.client.post(
            f"/v1/items/{item['id']}/dismiss", json={"reason": "me too"}, headers=headers
        )
        assert again.status_code == 200, again.text
        assert again.json()["item"]["dismissal"] == first["item"]["dismissal"]

    def test_work_that_fails_again_is_a_new_alert(self, api: Api) -> None:
        item = _blocked(api)
        headers = api.bearer()
        api.client.post(f"/v1/items/{item['id']}/dismiss", headers=headers)
        retried = api.client.post(f"/v1/items/{item['id']}/retry", headers=headers)
        assert retried.status_code == 200 and retried.json()["item"]["dismissal"] is None
        api.harness.outcomes = ["blocked"]
        assert api.loop.tick().outcome == "blocked"
        fresh = _item(api, item["id"], headers)
        assert fresh["state"] == "blocked" and fresh["dismissal"] is None
        assert "dismiss" in fresh["available_actions"]

    def test_a_dismissal_can_be_taken_back(self, api: Api) -> None:
        item = _blocked(api)
        headers = api.bearer()
        api.client.post(f"/v1/items/{item['id']}/dismiss", headers=headers)
        response = api.client.post(f"/v1/items/{item['id']}/undismiss", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["operation"]["action"] == "item.undismiss"
        assert response.json()["item"]["dismissal"] is None
        assert "dismiss" in response.json()["item"]["available_actions"]
        # Taking back what is not dismissed changes nothing and is no error.
        assert (
            api.client.post(f"/v1/items/{item['id']}/undismiss", headers=headers).status_code == 200
        )

    def test_work_that_raises_no_alert_is_refused_by_name(self, api: Api) -> None:
        api.harness.dstore.upsert_new(gh_item("1"), api.clock())
        headers = api.bearer()
        (item,) = api.client.get("/v1/items", headers=headers).json()["data"]
        assert "dismiss" not in item["available_actions"]
        response = api.client.post(f"/v1/items/{item['id']}/dismiss", headers=headers)
        assert response.status_code == 409 and response.json()["code"] == "not_eligible"
        assert response.json()["detail"] == "nothing needs attention: work item is queued"
        assert _item(api, item["id"], headers)["dismissal"] is None

    def test_it_is_guarded_like_every_other_command(self, api: Api) -> None:
        item = _blocked(api)
        headers = api.bearer()
        stale = api.client.post(
            f"/v1/items/{item['id']}/dismiss",
            json={"expected_revision": item["revision"] + 5},
            headers=headers,
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        reader = api.bearer(frozenset({"runs:read", "items:create", "runs:steer"}))
        refused = api.client.post(f"/v1/items/{item['id']}/dismiss", headers=reader)
        assert refused.status_code == 403 and refused.json()["capability"] == "runs:control"
        assert _item(api, item["id"], headers)["dismissal"] is None
        missing = api.client.post("/v1/items/itm_nope/dismiss", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"
        keyed = {**headers, "Idempotency-Key": "dm-1"}
        first = api.client.post(f"/v1/items/{item['id']}/dismiss", headers=keyed)
        replay = api.client.post(f"/v1/items/{item['id']}/dismiss", headers=keyed)
        assert first.status_code == replay.status_code == 200
        assert first.json()["operation"]["id"] == replay.json()["operation"]["id"]

    def test_a_gate_carries_its_item_s_dismissal(self, api: Api) -> None:
        from tests.api.test_control import gated

        gated(api)
        headers = api.bearer()
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        assert gate["dismissal"] is None
        dismissed = api.client.post(f"/v1/items/{gate['item_id']}/dismiss", headers=headers)
        assert dismissed.status_code == 200, dismissed.text
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        assert gate["dismissal"] == dismissed.json()["item"]["dismissal"]
        # The decision is still there to make.
        assert gate["state"] == "open" and gate["available_actions"] == ["approve"]


class TestARun:
    def test_a_run_its_item_pins_is_dismissed_on_the_item(self, api: Api) -> None:
        item = _blocked(api)
        headers = api.bearer()
        response = api.client.post(f"/v1/runs/{item['run_id']}/dismiss", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["operation"]["action"] == "run.dismiss"
        dismissal = response.json()["run"]["dismissal"]
        assert dismissal is not None
        assert _item(api, item["id"], headers)["dismissal"] == dismissal
        # One mark for the work: either route takes it back.
        undone = api.client.post(f"/v1/items/{item['id']}/undismiss", headers=headers)
        assert undone.json()["item"]["dismissal"] is None
        run = api.client.get(f"/v1/runs/{item['run_id']}", headers=headers).json()
        assert run["dismissal"] is None

    def test_a_run_nothing_pins_carries_its_own(self, api: Api) -> None:
        api.loop.store.create_run("standalone", "Compile a report", kind="tool")
        api.loop.store.set_run_state("standalone", "failed")
        headers = api.bearer()
        public = run_public("standalone")
        run = api.client.get(f"/v1/runs/{public}", headers=headers).json()
        assert run["item_id"] is None and "dismiss" in run["available_actions"]
        response = api.client.post(
            f"/v1/runs/{public}/dismiss",
            json={"expected_revision": run["revision"]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        dismissed = response.json()["run"]
        assert dismissed["dismissal"]["cause"] == "dismissed"
        assert dismissed["revision"] == run["revision"]
        assert "undismiss" in dismissed["available_actions"]
        assert "dismiss" not in dismissed["available_actions"]
        listed = api.client.get("/v1/runs", headers=headers).json()["data"]
        assert [row["dismissal"] for row in listed] == [dismissed["dismissal"]]
        # The run moves: what was acknowledged is no longer what it is.
        api.loop.store.set_run_state("standalone", "cancelled")
        assert api.client.get(f"/v1/runs/{public}", headers=headers).json()["dismissal"] is None

    def test_a_run_that_raises_no_alert_is_refused(self, api: Api) -> None:
        api.loop.store.create_run("fine", "Compile a report", kind="tool")
        api.loop.store.set_run_state("fine", "completed")
        headers = api.bearer()
        response = api.client.post(f"/v1/runs/{run_public('fine')}/dismiss", headers=headers)
        assert response.status_code == 409 and response.json()["code"] == "not_eligible"
        assert response.json()["detail"] == "nothing needs attention: run is completed"
        assert api.client.post("/v1/runs/run_nope/dismiss", headers=headers).status_code == 404
        reader = api.bearer(frozenset({"runs:read"}))
        refused = api.client.post(f"/v1/runs/{run_public('fine')}/dismiss", headers=reader)
        assert refused.status_code == 403


class TestAChannelsJobs:
    def test_a_job_row_carries_the_dismissal_of_its_work(self, api: Api) -> None:
        from tests.api.test_collaboration import bearer, register

        headers = bearer(register(api))
        stored = external_item(api)
        api.harness.dstore.mark_blocked(stored.item_id, "needs a decision", api.clock())
        api.ctx.project_work()
        (channel,) = channels(api)
        (job,) = api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json()
        assert job["state"] == "blocked" and job["dismissal"] is None
        assert "dismiss" in job["item_actions"]
        operator = api.bearer()
        dismissed = api.client.post(f"/v1/items/{job['item_id']}/dismiss", headers=operator)
        assert dismissed.status_code == 200, dismissed.text
        (job,) = api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json()
        assert job["dismissal"] == dismissed.json()["item"]["dismissal"]
        assert "undismiss" in job["item_actions"] and "dismiss" not in job["item_actions"]

    def test_a_job_with_no_item_is_dismissed_through_its_run(self, api: Api) -> None:
        from tests.api.test_collaboration import bearer, register

        headers = bearer(register(api))
        api.loop.store.create_run("standalone", "Compile a report", kind="tool")
        api.loop.store.set_run_state("standalone", "failed")
        api.ctx.project_work()
        (channel,) = channels(api)
        (job,) = api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json()
        assert job["item_id"] is None and job["item_actions"] == []
        assert "dismiss" in job["run_actions"] and job["dismissal"] is None
        dismissed = api.client.post(f"/v1/runs/{job['run_id']}/dismiss", headers=api.bearer())
        assert dismissed.status_code == 200, dismissed.text
        (job,) = api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json()
        assert job["dismissal"] == dismissed.json()["run"]["dismissal"]


def test_the_socket_takes_the_same_command(api: Api) -> None:
    item = _blocked(api)
    headers = api.bearer()
    with api.client.websocket_connect("/v1/ws", headers=headers) as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"
        ws.send_text(
            json.dumps(
                {
                    "type": "command",
                    "id": "d1",
                    "action": "item.dismiss",
                    "target": item["id"],
                    "params": {"reason": "seen"},
                    "expected_revision": item["revision"],
                }
            )
        )
        reply = json.loads(ws.receive_text())
        while reply.get("type") != "result" and "ok" not in reply:
            reply = json.loads(ws.receive_text())
        assert reply["ok"], reply
        assert reply["result"]["operation"]["action"] == "item.dismiss"
        assert reply["result"]["item"]["dismissal"]["reason"] == "seen"


def test_the_feature_is_advertised(api: Api) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "work.dismiss" in features
