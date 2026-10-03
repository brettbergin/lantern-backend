"""Deleting finished work: gone from every listing with its run directory,
its records kept, and never a way to stop something still in play."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from lantern.db.daemon_models import WorkItemRow
from lantern.db.engine_models import Run
from lantern.events import Event, HostEventTypes
from lantern.gc import workspace_pruned
from tests.api.conftest import Api
from tests.api.test_control import gated, run_public
from tests.api.test_external_work import channels, external_item
from tests.unit.test_daemon_loop import gh_item


def _blocked(api: Api, key: str = "1") -> tuple[dict[str, Any], str, Path]:
    """An item whose run ended blocked, with the run's directory on disk:
    the item as listed, the run id, the directory."""
    api.harness.source.items = [gh_item(key)]
    api.harness.outcomes = ["blocked"]
    assert api.loop.tick().outcome == "blocked"
    run_id = api.harness.runs[-1][0]
    run_dir = api.loop.config.paths.runs / run_id
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "workspace" / "file.bin").write_bytes(b"x" * 64)
    api.loop.store.set_run_workspace(run_id, run_dir / "workspace", mounted=True)
    listed = api.client.get("/v1/items", headers=api.bearer()).json()["data"]
    item = next(row for row in listed if row["origin"]["number"] == int(key))
    assert item["state"] == "blocked" and item["deleted_at"] is None
    return dict(item), run_id, run_dir


def _ids(api: Api, path: str, headers: dict[str, str]) -> list[str]:
    response = api.client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["data"]]


class TestAnItem:
    def test_it_leaves_every_listing_and_its_run_directory_goes(self, api: Api) -> None:
        item, run_id, run_dir = _blocked(api)
        headers = api.bearer()
        assert "delete" in item["available_actions"]
        response = api.client.post(
            f"/v1/items/{item['id']}/delete", json={"reason": "noise"}, headers=headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["operation"]["action"] == "item.delete"
        assert body["operation"]["state"] == "succeeded"
        assert body["operation"]["result"]["removed"] == [run_id]
        assert body["item"]["deleted_at"].endswith("Z")
        # Deleted work takes no further command.
        assert body["item"]["available_actions"] == []
        assert not run_dir.exists()
        # Gone from the listings; there again only when asked for.
        assert _ids(api, "/v1/items", headers) == []
        assert _ids(api, "/v1/runs", headers) == []
        assert _ids(api, "/v1/items?include_deleted=true", headers) == [item["id"]]
        assert _ids(api, "/v1/runs?include_deleted=true", headers) == [run_public(run_id)]
        # The records are the audit trail: still there, still readable by id.
        detail = api.client.get(f"/v1/items/{item['id']}", headers=headers).json()
        assert detail["state"] == "blocked" and detail["deleted_at"] == body["item"]["deleted_at"]
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        assert run["deleted_at"] is not None and run["available_actions"] == []
        with api.harness.dstore.read() as session:
            assert session.scalar(select(func.count()).select_from(WorkItemRow)) == 1
            assert session.scalar(select(func.count()).select_from(Run)) == 1
        events = api.client.get(f"/v1/runs/{run_public(run_id)}/events", headers=headers)
        assert events.status_code == 200 and events.json()["data"]
        # The removal is recorded as a sweep's is, so a resume is refused.
        assert workspace_pruned(api.loop.store, run_id)
        resumed = api.client.post(f"/v1/runs/{run_public(run_id)}/resume", headers=headers)
        assert resumed.status_code == 409 and resumed.json()["detail"] == "work was deleted"
        retried = api.client.post(f"/v1/items/{item['id']}/retry", headers=headers)
        assert retried.status_code == 409 and retried.json()["detail"] == "work was deleted"

    def test_deleting_twice_is_not_an_error(self, api: Api) -> None:
        item, _run_id, _run_dir = _blocked(api)
        headers = api.bearer()
        first = api.client.post(f"/v1/items/{item['id']}/delete", headers=headers)
        again = api.client.post(f"/v1/items/{item['id']}/delete", headers=headers)
        assert first.status_code == again.status_code == 200, again.text
        assert again.json()["item"]["deleted_at"] == first.json()["item"]["deleted_at"]

    def test_work_still_in_play_is_refused_by_name(self, api: Api) -> None:
        api.harness.dstore.upsert_new(gh_item("1"), api.clock())
        headers = api.bearer()
        (queued,) = api.client.get("/v1/items", headers=headers).json()["data"]
        assert "delete" not in queued["available_actions"]
        response = api.client.post(f"/v1/items/{queued['id']}/delete", headers=headers)
        assert response.status_code == 409 and response.json()["code"] == "not_eligible"
        assert response.json()["detail"] == "work item is queued; abandon it first"
        assert _ids(api, "/v1/items", headers) == [queued["id"]]

    def test_a_parked_decision_is_abandoned_before_it_is_deleted(self, api: Api) -> None:
        gated(api)
        headers = api.bearer()
        (item,) = api.client.get("/v1/items", headers=headers).json()["data"]
        refused = api.client.post(f"/v1/items/{item['id']}/delete", headers=headers)
        assert refused.status_code == 409
        assert refused.json()["detail"] == "work item is gated; abandon it first"
        abandoned = api.client.post(f"/v1/items/{item['id']}/abandon", headers=headers)
        assert "delete" in abandoned.json()["item"]["available_actions"]
        deleted = api.client.post(f"/v1/items/{item['id']}/delete", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert _ids(api, "/v1/items", headers) == []
        assert api.client.get("/v1/gates?state=open", headers=headers).json()["data"] == []

    def test_work_that_was_never_delivered_is_kept_unless_told_otherwise(self, api: Api) -> None:
        item, run_id, run_dir = _blocked(api)
        api.loop.store.append_event(
            Event.now(HostEventTypes.RUN_DELIVER, run_id, repo="o/r", error="HTTP 409")
        )
        headers = api.bearer()
        refused = api.client.post(f"/v1/items/{item['id']}/delete", headers=headers)
        assert refused.status_code == 409 and refused.json()["code"] == "not_eligible"
        assert "only copy" in refused.json()["detail"]
        # The flag a client offers "delete anyway" on, not the sentence.
        assert refused.json()["undelivered"] is True
        assert run_dir.exists() and _ids(api, "/v1/items", headers) == [item["id"]]
        forced = api.client.post(
            f"/v1/items/{item['id']}/delete", json={"discard_undelivered": True}, headers=headers
        )
        assert forced.status_code == 200, forced.text
        assert not run_dir.exists() and _ids(api, "/v1/items", headers) == []

    def test_it_is_guarded_like_every_other_command(self, api: Api) -> None:
        item, _run_id, run_dir = _blocked(api)
        headers = api.bearer()
        stale = api.client.post(
            f"/v1/items/{item['id']}/delete",
            json={"expected_revision": item["revision"] + 5},
            headers=headers,
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        reader = api.bearer(frozenset({"runs:read", "items:create", "runs:steer"}))
        forbidden = api.client.post(f"/v1/items/{item['id']}/delete", headers=reader)
        assert forbidden.status_code == 403 and forbidden.json()["capability"] == "runs:control"
        assert api.client.post("/v1/items/itm_nope/delete", headers=headers).status_code == 404
        assert run_dir.exists() and _ids(api, "/v1/items", headers) == [item["id"]]

    def test_a_page_is_full_without_the_deleted(self, api: Api) -> None:
        headers = api.bearer()
        for key in ("1", "2", "3"):
            api.clock.t += 1
            api.harness.dstore.upsert_new(gh_item(key), api.clock())
            api.harness.dstore.mark_blocked(f"gh:issue:{key}", "needs a decision", api.clock())
        newest, middle, oldest = _ids(api, "/v1/items", headers)
        assert api.client.post(f"/v1/items/{middle}/delete", headers=headers).status_code == 200
        page = api.client.get("/v1/items?limit=1", headers=headers).json()
        assert [row["id"] for row in page["data"]] == [newest] and page["has_more"]
        rest = api.client.get(
            f"/v1/items?limit=1&cursor={page['next_cursor']}", headers=headers
        ).json()
        assert [row["id"] for row in rest["data"]] == [oldest] and not rest["has_more"]

    def test_the_issue_asked_for_again_comes_back_and_its_old_run_stays_away(
        self, api: Api
    ) -> None:
        item, run_id, _run_dir = _blocked(api)
        headers = api.bearer()
        api.client.post(f"/v1/items/{item['id']}/delete", headers=headers)
        # The source admits it again (the trigger label came back).
        api.harness.dstore.retry("gh:issue:1", api.clock(), "re-queued by the source")
        (back,) = api.client.get("/v1/items", headers=headers).json()["data"]
        assert back["id"] == item["id"] and back["state"] == "queued"
        assert back["deleted_at"] is None
        assert _ids(api, "/v1/runs", headers) == []
        assert _ids(api, "/v1/runs?include_deleted=true", headers) == [run_public(run_id)]


class TestARun:
    def test_a_run_its_item_pins_deletes_the_work(self, api: Api) -> None:
        item, run_id, run_dir = _blocked(api)
        headers = api.bearer()
        response = api.client.post(f"/v1/runs/{run_public(run_id)}/delete", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["operation"]["action"] == "run.delete"
        assert response.json()["run"]["deleted_at"] is not None
        assert not run_dir.exists()
        assert _ids(api, "/v1/items", headers) == [] and _ids(api, "/v1/runs", headers) == []
        assert (
            api.client.get(f"/v1/items/{item['id']}", headers=headers).json()["deleted_at"]
            is not None
        )

    def test_a_run_nothing_pins_is_deleted_alone(self, api: Api) -> None:
        api.loop.store.create_run("standalone", "Compile a report", kind="tool")
        api.loop.store.set_run_state("standalone", "failed")
        api.loop.store.create_run("building", "Compile another", kind="tool")
        api.loop.store.set_run_state("building", "building")
        headers = api.bearer()
        public = run_public("standalone")
        run = api.client.get(f"/v1/runs/{public}", headers=headers).json()
        assert "delete" in run["available_actions"]
        deleted = api.client.post(f"/v1/runs/{public}/delete", headers=headers)
        assert deleted.status_code == 200, deleted.text
        assert _ids(api, "/v1/runs", headers) == [run_public("building")]
        refused = api.client.post(f"/v1/runs/{run_public('building')}/delete", headers=headers)
        assert refused.status_code == 409
        assert refused.json()["detail"] == "run is building; cancel it first"


class TestAChannelsJobs:
    def test_deleted_work_leaves_the_channel_s_list(self, api: Api) -> None:
        from tests.api.test_collaboration import bearer, register

        headers = bearer(register(api))
        stored = external_item(api)
        api.harness.dstore.mark_blocked(stored.item_id, "needs a decision", api.clock())
        api.ctx.project_work()
        (channel,) = channels(api)
        (job,) = api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json()
        assert "delete" in job["item_actions"]
        deleted = api.client.post(f"/v1/items/{job['item_id']}/delete", headers=api.bearer())
        assert deleted.status_code == 200, deleted.text
        assert api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json() == []
        assert api.client.get(f"/v1/channels/{channel.id}/work", headers=headers).json() == []

    def test_a_deleted_run_with_no_item_leaves_it_too(self, api: Api) -> None:
        from tests.api.test_collaboration import bearer, register

        headers = bearer(register(api))
        api.loop.store.create_run("standalone", "Compile a report", kind="tool")
        api.loop.store.set_run_state("standalone", "failed")
        api.ctx.project_work()
        (channel,) = channels(api)
        (job,) = api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json()
        assert "delete" in job["run_actions"]
        deleted = api.client.post(f"/v1/runs/{job['run_id']}/delete", headers=api.bearer())
        assert deleted.status_code == 200, deleted.text
        assert api.client.get(f"/v1/channels/{channel.id}/jobs", headers=headers).json() == []


def test_the_feature_is_advertised(api: Api) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "work.delete" in features
