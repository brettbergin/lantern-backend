"""What is waiting on a person, as one list: an open gate, an item parked
on a decision, work that ended needing someone, a failed task of an epic
run, and the daemon-level blocks only a person clears — each with a stable
id, its references, and the actions this caller may take."""

from __future__ import annotations

from typing import Any

from lantern.api.attention import ACTION_ORDER, capability_for
from lantern.api.models import rfc3339
from lantern.daemon.controls.eligibility import ACTIONS
from lantern.daemon.controls.principal import CAPABILITIES, ROLE_CAPABILITIES
from lantern.daemon.sources import RepoHealth
from lantern.provider import ProviderRecovery
from tests.api.conftest import Api, without_default_grants
from tests.api.test_channel_access import _channel, _invite
from tests.api.test_collaboration import bearer, register
from tests.api.test_control import gated, run_public
from tests.api.test_plans_run import READ, _control, _started, _task
from tests.api.test_read_path_cost import statements
from tests.fakes.fake_github import FakeGithub
from tests.fakes.rawdb import exec_raw
from tests.unit.test_daemon_loop import PR_URL, FakeSource, gh_item
from tests.unit.test_daemon_merge_gate import FakeDaemonGithub

NOTHING = {"total": 0, "decision": 0, "failed": 0, "paused": 0}


def _attention(api: Api, headers: dict[str, str] | None = None, **params: Any) -> dict[str, Any]:
    response = api.client.get("/v1/attention", params=params, headers=headers or api.bearer())
    assert response.status_code == 200, response.text
    return dict(response.json())


def _entries(api: Api, headers: dict[str, str] | None = None, **params: Any) -> list[Any]:
    return list(_attention(api, headers, **params)["data"])


def _blocked(api: Api, key: str = "1") -> str:
    """An item whose run ended blocked; the run id."""
    api.harness.source.items = [gh_item(key)]
    api.harness.outcomes = ["blocked"]
    api.clock.t += 10
    assert api.loop.tick().outcome == "blocked"
    return str(api.harness.runs[-1][0])


def _parked(api: Api, key: str, state: str = "blocked", **fields: Any) -> None:
    """An item resting in ``state`` that never had a run."""
    dstore = api.harness.dstore
    api.clock.t += 10
    dstore.upsert_new(gh_item(key, **fields), api.clock())
    stored = next(item.item_id for item in dstore.items() if item.source_key == key)
    if state == "blocked":
        dstore.mark_blocked(stored, "the checks never went green", api.clock())
    elif state == "failed":
        dstore.mark_failed(stored, "gave up", api.clock(), requeue=False)
    else:
        dstore.mark_cancelled(stored, "stopped", api.clock())


def _awaiting_review(api: Api) -> str:
    fake = FakeGithub(number=9)
    fake.pr["html_url"] = PR_URL
    api.loop.github = FakeDaemonGithub(fake)  # type: ignore[assignment]
    api.harness.source.items = [gh_item("1")]
    api.harness.outcomes = ["awaiting_review"]
    api.clock.t += 10
    api.loop.tick()
    return str(api.harness.runs[-1][0])


def _item(api: Api, number: int = 1) -> dict[str, Any]:
    listed = api.client.get(
        "/v1/items", params={"include_deleted": True}, headers=api.bearer()
    ).json()["data"]
    return dict(next(row for row in listed if row["origin"]["number"] == number))


def _actions(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {action["action"]: action for action in entry["actions"]}


class SuspendedSource(FakeSource):
    """A source one of whose repositories stopped being polled."""

    def __init__(self, health: RepoHealth) -> None:
        super().__init__()
        self.health = health

    @property
    def repo_health(self) -> list[RepoHealth]:
        return [self.health]


def test_an_empty_workspace_has_nothing_waiting(api: Api) -> None:
    body = _attention(api)
    assert body["data"] == [] and body["counts"] == NOTHING
    assert body["next_cursor"] is None and body["has_more"] is False
    assert body["observed_at"] == rfc3339(api.clock())
    # Work that asks nothing of anyone is not on the list: queued, done.
    api.harness.dstore.upsert_new(gh_item("1"), api.clock())
    api.harness.source.items = [gh_item("2")]
    assert api.loop.tick().outcome == "done"
    assert _attention(api)["counts"] == NOTHING


class TestAGate:
    def test_a_gated_run_is_one_entry_keyed_by_its_gate(self, api: Api) -> None:
        run_id = gated(api)
        (gate,) = api.client.get("/v1/gates", headers=api.bearer()).json()["data"]
        body = _attention(api)
        # The gated item is the same waiting thing: one entry, not two.
        (entry,) = body["data"]
        assert entry["id"] == f"gate:{gate['id']}" and entry["kind"] == "gate"
        assert entry["group"] == "decision" and entry["state"] == "gated"
        assert entry["title"] == "Do 1" and entry["repository"] == "o/r"
        assert entry["repository_id"].startswith("repo_")
        assert entry["gate_id"] == gate["id"] and entry["item_id"] == gate["item_id"]
        assert entry["run_id"] == run_public(run_id)
        assert entry["revision"] == gate["revision"] and entry["since"] == gate["created_at"]
        assert entry["plan_id"] is None and entry["node_id"] is None
        assert entry["epic_run_id"] is None and entry["dismissal"] is None
        assert body["counts"] == {**NOTHING, "total": 1, "decision": 1}
        # The same id on the next read: the thing waiting has not changed.
        assert _entries(api)[0]["id"] == entry["id"]

    def test_approving_is_advertised_to_an_approver_and_withheld_from_a_member(
        self, api: Api
    ) -> None:
        gated(api)
        (entry,) = _entries(api, api.bearer(ROLE_CAPABILITIES["admin"]))
        approve = entry["actions"][0]
        assert approve == {"action": "gate_approve", "capability": "gates:approve", "allowed": True}
        assert all(action["allowed"] for action in entry["actions"])
        # A member reads the same entry and the same actions, none of them theirs.
        (seen,) = _entries(api, api.bearer(ROLE_CAPABILITIES["member"]))
        assert [a["action"] for a in seen["actions"]] == [a["action"] for a in entry["actions"]]
        assert _actions(seen)["gate_approve"]["allowed"] is False
        assert not any(action["allowed"] for action in seen["actions"])
        assert {**seen, "actions": []} == {**entry, "actions": []}

    def test_a_held_publication_is_an_entry_too(self, api: Api) -> None:
        gated(api, kind="publish")
        (gate,) = api.client.get("/v1/gates", headers=api.bearer()).json()["data"]
        (entry,) = _entries(api)
        assert entry["id"] == f"gate:{gate['id']}" and entry["group"] == "decision"
        assert "gate_approve" in _actions(entry)

    def test_an_approved_gate_is_no_longer_waiting_on_anyone(self, api: Api) -> None:
        run_id = gated(api)
        assert api.harness.dstore.claim_merge_gate(run_id, "ana")
        assert _attention(api)["counts"] == NOTHING

    def test_a_dismissed_gate_leaves_the_list_until_it_is_asked_for(self, api: Api) -> None:
        gated(api)
        (entry,) = _entries(api)
        dismissed = api.client.post(f"/v1/items/{entry['item_id']}/dismiss", headers=api.bearer())
        assert dismissed.status_code == 200, dismissed.text
        assert _attention(api)["counts"] == NOTHING
        (shown,) = _entries(api, include_dismissed=True)
        assert shown["id"] == entry["id"]
        assert shown["dismissal"] == dismissed.json()["item"]["dismissal"]
        assert "undismiss" in _actions(shown) and "gate_approve" in _actions(shown)


class TestAParkedItem:
    def test_a_run_waiting_for_a_review_is_a_decision(self, api: Api) -> None:
        run_id = _awaiting_review(api)
        item = _item(api)
        (entry,) = _entries(api)
        assert entry["kind"] == "item" and entry["group"] == "decision"
        assert entry["state"] == "awaiting_review"
        assert entry["id"] == f"item:{item['id']}:awaiting_review:{run_public(run_id)}"
        assert entry["item_id"] == item["id"] and entry["run_id"] == run_public(run_id)
        assert entry["gate_id"] is None and entry["revision"] == item["revision"]
        assert entry["since"] == item["updated_at"]
        resume = _actions(entry)["review_wait_resume"]
        assert resume == {
            "action": "review_wait_resume",
            "capability": "runs:control",
            "allowed": True,
        }

    def test_a_review_wait_that_paused_is_a_different_entry(self, api: Api) -> None:
        run_id = _awaiting_review(api)
        (waiting,) = _entries(api)
        api.clock.t += 60
        api.harness.dstore.pause_review_hold(run_id, api.clock(), "no review in a day")
        api.harness.dstore.mark_paused_review("gh:1", "no review in a day", api.clock())
        (paused,) = _entries(api)
        assert paused["state"] == "paused_review" and paused["group"] == "decision"
        assert paused["reason"] == "no review in a day"
        assert paused["id"] != waiting["id"] and paused["since"] != waiting["since"]
        assert "review_wait_resume" in _actions(paused)

    def test_an_item_waiting_for_answers_is_a_decision(self, api: Api) -> None:
        api.harness.dstore.upsert_new(gh_item("1"), api.clock())
        api.harness.dstore.mark_awaiting_answers("gh:1", api.clock())
        (entry,) = _entries(api)
        assert entry["state"] == "awaiting_answers" and entry["group"] == "decision"
        assert entry["id"] == f"item:{entry['item_id']}:awaiting_answers"
        assert entry["run_id"] is None


class TestWorkThatEndedNeedingAPerson:
    def test_a_failure_waits_until_it_is_dismissed_or_retried(self, api: Api) -> None:
        run_id = _blocked(api)
        item = _item(api)
        headers = api.bearer()
        body = _attention(api)
        (entry,) = body["data"]
        assert entry["id"] == f"item:{item['id']}:blocked:{run_public(run_id)}"
        assert entry["kind"] == "item" and entry["group"] == "failed"
        assert entry["state"] == "blocked" and entry["title"] == "Do 1"
        assert entry["reason"] == item["last_error"] and entry["reason"]
        assert entry["revision"] == item["revision"] and entry["since"] == item["updated_at"]
        assert [a["action"] for a in entry["actions"]] == ["retry", "abandon", "dismiss", "delete"]
        assert {a["capability"] for a in entry["actions"]} == {"runs:control"}
        assert body["counts"] == {**NOTHING, "total": 1, "failed": 1}

        # Dismissed: nobody is asked any more — unless they ask to see it.
        dismissed = api.client.post(f"/v1/items/{item['id']}/dismiss", headers=headers)
        assert dismissed.status_code == 200, dismissed.text
        assert _attention(api)["counts"] == NOTHING
        shown = _attention(api, include_dismissed=True)
        assert shown["counts"] == {**NOTHING, "total": 1, "failed": 1}
        assert shown["data"][0]["id"] == entry["id"]
        assert shown["data"][0]["dismissal"] == dismissed.json()["item"]["dismissal"]
        assert "undismiss" in _actions(shown["data"][0])

        # Retried: the work is moving, and nothing about it waits on anyone.
        retried = api.client.post(f"/v1/items/{item['id']}/retry", headers=headers)
        assert retried.status_code == 200, retried.text
        assert _attention(api, include_dismissed=True)["counts"] == NOTHING

        # Failing again is a new alert: back on the list, under a new id.
        api.harness.outcomes = ["blocked"]
        api.clock.t += 10
        assert api.loop.tick().outcome == "blocked"
        (fresh,) = _entries(api)
        assert fresh["item_id"] == item["id"] and fresh["dismissal"] is None
        assert fresh["id"] != entry["id"] and fresh["run_id"] != entry["run_id"]

    def test_a_deleted_item_never_appears(self, api: Api) -> None:
        _blocked(api)
        item = _item(api)
        deleted = api.client.post(f"/v1/items/{item['id']}/delete", headers=api.bearer())
        assert deleted.status_code == 200, deleted.text
        assert _attention(api)["counts"] == NOTHING
        assert _attention(api, include_dismissed=True)["counts"] == NOTHING

    def test_a_failed_item_is_an_entry_and_a_cancelled_one_is_not(self, api: Api) -> None:
        _parked(api, "1", "failed")
        _parked(api, "2", "cancelled")
        (entry,) = _entries(api, include_dismissed=True)
        assert entry["state"] == "failed" and entry["item_id"] == _item(api, 1)["id"]
        assert entry["id"] == f"item:{entry['item_id']}:failed" and entry["run_id"] is None

    def test_work_a_person_gave_up_is_not_waiting_on_anyone(self, api: Api) -> None:
        _parked(api, "1")
        item = _item(api)
        abandoned = api.client.post(f"/v1/items/{item['id']}/abandon", headers=api.bearer())
        assert abandoned.status_code == 200, abandoned.text
        assert _attention(api)["counts"] == NOTHING
        # It is there for whoever asks, saying it was given up, not left to fail.
        (shown,) = _entries(api, include_dismissed=True)
        assert shown["state"] == "failed" and shown["dismissal"]["cause"] == "abandoned"

    def test_the_conversation_is_named_only_to_a_reader_who_can_open_it(self, api: Api) -> None:
        owner = register(api)
        guest = _invite(api, "member", "guest")
        channel = _channel(api, bearer(owner))
        api.harness.dstore.upsert_new(gh_item("1", channel_id=channel), api.clock())
        api.harness.dstore.mark_blocked("gh:1", "stuck", api.clock())
        (own,) = _entries(api, bearer(owner))
        assert own["channel_id"] == channel
        # The guest sees the work — the list is workspace-wide — without the link.
        (seen,) = _entries(api, bearer(guest))
        assert seen["id"] == own["id"] and seen["channel_id"] is None


class TestAnEpicRun:
    def test_a_failed_task_is_one_entry_carrying_the_run_and_the_item(self, api: Api) -> None:
        _, headers, plan, run = _started(api)
        task = _task(run, "A")
        api.loop.dstore.abandon(task["item_id"], "the tests failed", api.clock())
        api.loop.epic_runs.tick(api.clock())
        read = api.client.get(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/run", headers=api.bearer(READ)
        ).json()
        assert _task(read, "A")["state"] == "failed" and _task(read, "B")["state"] == "blocked"
        # The run is still `running`: the entry is the task, and its failed
        # item is the same waiting thing. The task blocked behind it waits
        # on the same decision and is no second entry.
        assert read["state"] == "running"
        body = _attention(api)
        (entry,) = body["data"]
        assert entry["kind"] == "epic_task" and entry["group"] == "failed"
        assert entry["id"] == f"epic_task:{run['id']}:{task['node_id']}"
        assert entry["state"] == "failed" and entry["title"] == "A"
        assert entry["epic_run_id"] == run["id"] and entry["plan_id"] == plan["id"]
        assert entry["node_id"] == task["node_id"] and entry["repository"] == "o/r"
        assert entry["item_id"] == _item(api, _task(read, "A")["forge"]["number"])["id"]
        assert entry["reason"] == _task(read, "A")["reason"]
        assert entry["since"] == _task(read, "A")["updated_at"]
        assert entry["actions"] == [
            {"action": "task_retry", "capability": "plans:publish", "allowed": True},
            {"action": "task_skip", "capability": "plans:publish", "allowed": True},
        ]
        assert body["counts"] == {**NOTHING, "total": 1, "failed": 1}
        (seen,) = _entries(api, api.bearer(ROLE_CAPABILITIES["member"]))
        assert not any(action["allowed"] for action in seen["actions"])

        # Dismissing the item's alert does not unblock the run: it still waits.
        dismissed = api.client.post(f"/v1/items/{entry['item_id']}/dismiss", headers=api.bearer())
        assert dismissed.status_code == 200, dismissed.text
        (still,) = _entries(api)
        assert still["id"] == entry["id"] and still["dismissal"] is None

        # Retried through the run, the task is moving and nothing waits.
        retried = _control(api, headers, plan, "retry", task["node_id"], key="c2")
        assert retried.status_code == 200, retried.text
        assert _attention(api, include_dismissed=True)["counts"] == NOTHING

    def test_a_stopped_run_leaves_only_the_failed_item(self, api: Api) -> None:
        _, headers, plan, run = _started(api)
        task = _task(run, "A")
        api.loop.dstore.abandon(task["item_id"], "the tests failed", api.clock())
        api.loop.epic_runs.tick(api.clock())
        assert _control(api, headers, plan, "cancel").status_code == 200
        (entry,) = _entries(api)
        assert entry["kind"] == "item" and entry["state"] == "failed"
        # The item still says which run admitted it.
        assert entry["epic_run_id"] == run["id"]


class TestDaemonLevelBlocks:
    def _recovery(self, api: Api) -> ProviderRecovery:
        return ProviderRecovery(api.harness.store, api.loop.config.agent.backend, clock=api.clock)

    def test_a_provider_hold_only_a_person_clears_is_an_entry(self, api: Api) -> None:
        backend = api.loop.config.agent.backend
        self._recovery(api).park_recovery("inspect the preserved checkpoint")
        hold = self._recovery(api).hold()
        assert hold is not None and hold.next_at is None
        body = _attention(api)
        (entry,) = body["data"]
        assert entry["kind"] == "provider_hold" and entry["group"] == "paused"
        assert entry["id"] == f"provider_hold:{backend}:{hold.generation}"
        assert entry["state"] == "provider_held" and backend in entry["title"]
        assert entry["reason"] == hold.summary()
        assert "explicit operator recovery required" in entry["reason"]
        assert entry["since"] == rfc3339(api.clock())
        assert entry["repository"] is None and entry["revision"] is None
        # No route releases it: it is cleared from the host or a chat.
        assert entry["actions"] == []
        assert body["counts"] == {**NOTHING, "total": 1, "paused": 1}

    def test_a_provider_hold_with_a_scheduled_retry_is_not(self, api: Api) -> None:
        self._recovery(api).park_recovery("throttled")
        exec_raw(api.harness.store, "UPDATE provider_holds SET next_at=?", (api.clock() + 300,))
        hold = self._recovery(api).hold()
        assert hold is not None and hold.next_at is not None
        assert _attention(api)["counts"] == NOTHING

    def test_a_suspended_repository_is_an_entry(self, api: Api) -> None:
        api.loop.source = SuspendedSource(RepoHealth("o/r", 4, None, True, "gone", 1.0))
        (repo,) = api.client.get("/v1/repositories", headers=api.bearer()).json()["data"]
        (entry,) = _entries(api)
        assert entry["kind"] == "repository" and entry["group"] == "paused"
        assert entry["id"] == f"repository:{repo['id']}" and entry["state"] == "suspended"
        assert entry["repository"] == "o/r" and entry["repository_id"] == repo["id"]
        assert entry["reason"] == "gone" and entry["since"] == rfc3339(1.0)
        assert entry["actions"] == [
            {"action": "repository_resume", "capability": "daemon:manage", "allowed": True}
        ]
        (seen,) = _entries(api, api.bearer(ROLE_CAPABILITIES["member"]))
        assert seen["actions"][0]["allowed"] is False

    def test_a_repository_backing_off_and_a_named_hold_are_not(self, api: Api) -> None:
        api.loop.source = SuspendedSource(RepoHealth("o/r", 2, api.clock() + 60, False, "5xx", 1.0))
        held = api.client.post(
            "/v1/daemon/holds",
            json={"name": "deploy-1", "reason": "rolling out"},
            headers=api.bearer(),
        )
        assert held.status_code in (200, 201), held.text
        assert api.loop.paused
        assert _attention(api)["counts"] == NOTHING


class TestTheList:
    def _mixed(self, api: Api) -> None:
        """Two failures a minute apart, a gate opened after both, and a
        suspended repository."""
        # The list's order, not triage: no default grant judges the failures.
        without_default_grants(api)
        _parked(api, "2", repo="o/r")
        api.clock.t += 60
        _parked(api, "3", "failed", repo="o/r")
        api.clock.t += 60
        gated(api)
        api.loop.source = SuspendedSource(RepoHealth("o/r", 4, None, True, "gone", 1.0))

    def test_decisions_come_first_then_failures_then_pauses_oldest_first(self, api: Api) -> None:
        self._mixed(api)
        body = _attention(api)
        assert [(e["kind"], e["group"]) for e in body["data"]] == [
            ("gate", "decision"),
            ("item", "failed"),
            ("item", "failed"),
            ("repository", "paused"),
        ]
        older, newer = body["data"][1], body["data"][2]
        assert older["item_id"] == _item(api, 2)["id"] and newer["item_id"] == _item(api, 3)["id"]
        assert older["since"] < newer["since"]
        assert body["counts"] == {"total": 4, "decision": 1, "failed": 2, "paused": 1}

    def test_a_group_filter_narrows_the_page_and_never_the_counts(self, api: Api) -> None:
        self._mixed(api)
        failed = _attention(api, group="failed")
        assert [e["group"] for e in failed["data"]] == ["failed", "failed"]
        assert failed["counts"] == {"total": 4, "decision": 1, "failed": 2, "paused": 1}
        both = _attention(api, group=["decision", "paused"])
        assert [e["group"] for e in both["data"]] == ["decision", "paused"]
        refused = api.client.get("/v1/attention", params={"group": "odd"}, headers=api.bearer())
        assert refused.status_code == 422 and refused.json()["code"] == "invalid_request"

    def test_a_repository_filter_keeps_what_belongs_to_it(self, api: Api) -> None:
        self._mixed(api)
        ProviderRecovery(
            api.harness.store, api.loop.config.agent.backend, clock=api.clock
        ).park_recovery("inspect")
        (repo,) = api.client.get("/v1/repositories", headers=api.bearer()).json()["data"]
        _parked(api, "4", repo="o/other")
        assert _attention(api)["counts"]["total"] == 6
        body = _attention(api, repository_id=repo["id"])
        # The provider hold belongs to no repository, and one item to another.
        assert [e["kind"] for e in body["data"]] == ["gate", "item", "item", "repository"]
        assert {e["repository_id"] for e in body["data"]} == {repo["id"]}
        assert body["counts"] == {"total": 4, "decision": 1, "failed": 2, "paused": 1}
        missing = api.client.get(
            "/v1/attention", params={"repository_id": "repo_nope"}, headers=api.bearer()
        )
        assert missing.status_code == 404 and missing.json()["code"] == "not_found"

    def test_it_pages_in_order_with_the_counts_on_every_page(self, api: Api) -> None:
        for key in ("1", "2", "3"):
            _parked(api, key)
        whole = _entries(api)
        assert len(whole) == 3
        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            params: dict[str, Any] = {"limit": 1}
            if cursor is not None:
                params["cursor"] = cursor
            page = _attention(api, **params)
            assert len(page["data"]) == 1 and page["counts"]["total"] == 3
            seen.append(page["data"][0]["id"])
            cursor = page["next_cursor"]
            assert page["has_more"] is (cursor is not None)
        assert seen == [entry["id"] for entry in whole] and cursor is None
        # A cursor belongs to the listing that issued it.
        first = _attention(api, limit=1)["next_cursor"]
        other = api.client.get(
            "/v1/attention", params={"cursor": first, "group": "failed"}, headers=api.bearer()
        )
        assert other.status_code == 400 and other.json()["code"] == "invalid_cursor"
        junk = api.client.get("/v1/attention", params={"cursor": "junk"}, headers=api.bearer())
        assert junk.status_code == 400 and junk.json()["code"] == "invalid_cursor"

    def test_a_page_costs_the_same_however_much_is_waiting_behind_it(self, api: Api) -> None:
        """The whole list is read to count it, in a fixed number of
        statements; only the page is projected. A badge poll (``limit=1``)
        must not cost more as the alerts pile up."""
        headers = api.bearer()
        _parked(api, "1")
        _attention(api, headers, limit=1)  # mints the page's public ids
        with statements(api) as seen:
            assert _attention(api, headers, limit=1)["counts"]["total"] == 1
            few = len(seen)
        for key in ("2", "3", "4", "5", "6"):
            _parked(api, key)
        _attention(api, headers, limit=1)
        with statements(api) as seen:
            assert _attention(api, headers, limit=1)["counts"]["total"] == 6
            many = len(seen)
        assert few == many, f"{few} statements with one alert, {many} with six"

    def test_reading_it_needs_runs_read(self, api: Api) -> None:
        refused = api.client.get("/v1/attention", headers=api.bearer(frozenset({"items:create"})))
        assert refused.status_code == 403 and refused.json()["capability"] == "runs:read"
        assert api.client.get("/v1/attention").status_code == 401


def test_every_action_the_daemon_can_advertise_has_an_order_and_a_capability() -> None:
    """An action eligibility learns later must not be dropped from an
    entry, or advertised without the capability it needs."""
    assert set(ACTION_ORDER) == set(ACTIONS) and len(ACTION_ORDER) == len(ACTIONS)
    for action in (*ACTIONS, "task_retry", "task_skip", "repository_resume"):
        assert capability_for(action) in CAPABILITIES


def test_the_feature_is_advertised(api: Api) -> None:
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "attention" in features
