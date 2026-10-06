"""Acting on what is waiting, by the entry: ``POST /v1/attention/{id}/act``
routes an action the entry offers to the command its own route runs — the
same operation, the same refusals — for a caller holding nothing but the
entry's id and the action's name."""

from __future__ import annotations

from typing import Any

import pytest

from lantern.api import attention_act
from lantern.api.attention import ACTION_ORDER, REPOSITORY_ACTIONS, TASK_ACTIONS
from lantern.daemon.controls.operations import EFFECTS
from lantern.daemon.controls.principal import ROLE_CAPABILITIES
from lantern.daemon.sources import RepoHealth
from tests.api.conftest import Api
from tests.api.test_attention import _awaiting_review, _blocked, _entries, _parked
from tests.api.test_control import gated, landed, run_public
from tests.api.test_plans_run import READ, _started, _task
from tests.api.test_read_path_cost import statements
from tests.unit.test_daemon_loop import FakeSource, gh_item


def _act(
    api: Api,
    headers: dict[str, str],
    entry_id: str,
    action: str,
    key: str | None = "k1",
    **body: Any,
) -> Any:
    extra = {} if key is None else {"Idempotency-Key": key}
    return api.client.post(
        f"/v1/attention/{entry_id}/act",
        json={"action": action, **body},
        headers={**headers, **extra},
    )


def _operations(api: Api) -> list[dict[str, Any]]:
    listed = api.client.get("/v1/operations", params={"limit": 200}, headers=api.bearer())
    assert listed.status_code == 200, listed.text
    return list(listed.json()["data"])


def _recorded(api: Api, action: str) -> list[dict[str, Any]]:
    return [op for op in _operations(api) if op["action"] == action]


class ResumableSource(FakeSource):
    """A source whose one repository stopped being polled until resumed."""

    def __init__(self) -> None:
        super().__init__()
        self.health = RepoHealth("o/r", 4, None, True, "gone", 1.0)

    @property
    def repo_health(self) -> list[RepoHealth]:
        return [self.health]

    def resume_repo(self, repo: str) -> RepoHealth:
        if self.health.state == "ok":
            raise ValueError("o/r is not suspended or backing off")
        self.health = RepoHealth("o/r")
        return self.health


def _failed_task(api: Api) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """An epic run whose task A failed; the plan, the run and the entry."""
    _, _, plan, run = _started(api)
    task = _task(run, "A")
    api.loop.dstore.abandon(task["item_id"], "the tests failed", api.clock())
    api.loop.epic_runs.tick(api.clock())
    (entry,) = _entries(api)
    assert entry["kind"] == "epic_task" and entry["node_id"] == task["node_id"]
    return plan, run, entry


class TestAGate:
    def test_approving_needs_the_revision_the_person_saw(self, api: Api) -> None:
        run_id = gated(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        (gate,) = api.client.get("/v1/gates", headers=headers).json()["data"]
        # Never defaulted from the entry as it stands now: the person says
        # what they were looking at.
        missing = _act(api, headers, entry["id"], "gate_approve", key="g0")
        assert missing.status_code == 422 and missing.json()["code"] == "invalid_request"
        assert missing.json()["errors"][0]["loc"] == ["expected_revision"]
        assert _recorded(api, "gate.approve") == []
        # A gate that moved is refused as its own route refuses it.
        stale = _act(
            api,
            headers,
            entry["id"],
            "gate_approve",
            key="g1",
            expected_revision=entry["revision"] + 1,
        )
        direct = api.client.post(
            f"/v1/gates/{gate['id']}/approve",
            json={"expected_revision": gate["revision"] + 1},
            headers=headers,
        )
        assert stale.status_code == direct.status_code == 409
        assert stale.json()["code"] == direct.json()["code"] == "stale_revision"
        assert stale.json()["revision"] == direct.json()["revision"] == gate["revision"]

        approved = _act(
            api, headers, entry["id"], "gate_approve", key="g2", expected_revision=entry["revision"]
        )
        assert approved.status_code == 202, approved.text
        body = approved.json()
        assert body["entry_id"] == entry["id"] and body["action"] == "gate_approve"
        assert body["result"]["operation"]["action"] == "gate.approve"
        assert body["operation_id"] == body["result"]["operation"]["id"]
        assert body["result"]["gate"]["id"] == gate["id"]
        assert body["result"]["gate"]["state"] in ("approving", "merged")
        assert "approved by" in body["result"]["message"]
        assert body["still_waiting"] is False and body["replayed"] is False
        landed(api, run_id)
        final = api.client.get(f"/v1/gates/{gate['id']}", headers=headers).json()
        assert final["state"] == "merged"
        assert _entries(api) == []

    def test_an_action_on_the_gated_item_is_checked_against_the_entry(self, api: Api) -> None:
        """The entry's revision is the gate's; dismissing is the item's
        command. The entry the person saw is what the revision pins."""
        gated(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        moved = _act(
            api, headers, entry["id"], "dismiss", key="d0", expected_revision=entry["revision"] + 1
        )
        assert moved.status_code == 409 and moved.json()["code"] == "stale_revision"
        assert moved.json()["revision"] == entry["revision"]
        assert _recorded(api, "item.dismiss") == []
        dismissed = _act(
            api, headers, entry["id"], "dismiss", key="d1", expected_revision=entry["revision"]
        )
        assert dismissed.status_code == 200, dismissed.text
        assert dismissed.json()["result"]["item"]["dismissal"]["cause"] == "dismissed"
        assert dismissed.json()["still_waiting"] is False


class TestAnItem:
    def test_a_failed_item_is_retried(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        stale = _act(
            api, headers, entry["id"], "retry", key="r0", expected_revision=entry["revision"] + 1
        )
        assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
        retried = _act(
            api, headers, entry["id"], "retry", key="r1", expected_revision=entry["revision"]
        )
        assert retried.status_code == 200, retried.text
        body = retried.json()
        assert body["entry_id"] == entry["id"] and body["action"] == "retry"
        assert body["result"]["item"]["id"] == entry["item_id"]
        assert body["result"]["item"]["state"] == "queued"
        assert body["result"]["operation"]["action"] == "item.retry"
        assert body["operation_id"] == body["result"]["operation"]["id"]
        assert body["still_waiting"] is False
        assert _entries(api, include_dismissed=True) == []

    def test_dismissing_puts_the_alert_away_and_undismissing_brings_it_back(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        dismissed = _act(
            api, headers, entry["id"], "dismiss", key="d1", params={"reason": "known flake"}
        )
        assert dismissed.status_code == 200, dismissed.text
        body = dismissed.json()
        assert body["result"]["operation"]["action"] == "item.dismiss"
        assert body["result"]["item"]["dismissal"]["reason"] == "known flake"
        # Still listed for whoever asks, and no longer waiting on anyone.
        assert body["still_waiting"] is False
        assert _entries(api) == []
        # The same id still names it: the entry is found among the dismissed.
        back = _act(api, headers, entry["id"], "undismiss", key="d2")
        assert back.status_code == 200, back.text
        assert back.json()["result"]["item"]["dismissal"] is None
        assert back.json()["still_waiting"] is True
        assert [e["id"] for e in _entries(api)] == [entry["id"]]

    def test_rounds_are_granted_with_the_actions_own_arguments(self, api: Api) -> None:
        # Out of fix rounds twice over: the item rests failed, asking.
        api.harness.source.items = [gh_item("1")]
        api.harness.outcomes = ["exhausted", "exhausted"]
        for _ in range(2):
            api.clock.t += 1000
            api.loop.tick()
        run_id = api.harness.runs[-1][0]
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        assert "grant_rounds" in {a["action"] for a in entry["actions"]}
        run = api.client.get(f"/v1/runs/{run_public(run_id)}", headers=headers).json()
        # Without the grant's own capability the act is refused by name.
        reader = api.bearer(frozenset({"runs:read", "runs:control"}))
        refused = _act(api, reader, entry["id"], "grant_rounds", params={"rounds": 1})
        assert refused.status_code == 403 and refused.json()["capability"] == "budgets:grant"
        # The same body model the grant's own route validates with.
        for key, params in (("n0", {}), ("n1", {"rounds": 0}), ("n2", {"rounds": 1, "odd": True})):
            invalid = _act(api, headers, entry["id"], "grant_rounds", key=key, params=params)
            assert invalid.status_code == 422, invalid.text
            assert invalid.json()["code"] == "invalid_request"
            assert invalid.json()["errors"][0]["loc"][0] == "params"
        assert _recorded(api, "run.grant_rounds") == []
        granted = _act(api, headers, entry["id"], "grant_rounds", key="n3", params={"rounds": 2})
        assert granted.status_code == 200, granted.text
        body = granted.json()
        assert body["result"]["operation"]["action"] == "run.grant_rounds"
        assert body["result"]["run"]["rounds"]["granted"] == run["rounds"]["granted"] + 2
        assert body["still_waiting"] is False
        item = api.loop.dstore.get("gh:issue:1")
        assert item is not None and item.state == "queued" and item.run_id == run_id

    def test_a_run_action_is_checked_against_the_entry_the_person_saw(self, api: Api) -> None:
        """The entry's revision is the item's; re-arming the review wait is
        the run's command and takes none. The entry is what is pinned."""
        _awaiting_review(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        moved = _act(
            api,
            headers,
            entry["id"],
            "review_wait_resume",
            key="w0",
            expected_revision=entry["revision"] + 1,
        )
        assert moved.status_code == 409 and moved.json()["code"] == "stale_revision"
        assert moved.json()["revision"] == entry["revision"]
        assert _recorded(api, "run.review_resume") == []
        resumed = _act(
            api,
            headers,
            entry["id"],
            "review_wait_resume",
            key="w1",
            expected_revision=entry["revision"],
        )
        assert resumed.status_code == 200, resumed.text
        body = resumed.json()
        assert body["result"]["operation"]["action"] == "run.review_resume"
        assert "waiting for a review" in body["result"]["message"]
        # Re-armed, the review is still a person's to give.
        assert body["still_waiting"] is True
        extra = _act(
            api, headers, entry["id"], "review_wait_resume", key="w2", params={"reason": "x"}
        )
        assert extra.status_code == 422 and extra.json()["code"] == "invalid_request"


class TestARepository:
    def test_a_suspended_repository_is_resumed(self, api: Api) -> None:
        api.loop.source = ResumableSource()
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        assert entry["kind"] == "repository" and entry["revision"] is None
        # Nothing about a repository has a revision to pin.
        pinned = _act(api, headers, entry["id"], "repository_resume", key="p0", expected_revision=1)
        assert pinned.status_code == 422 and pinned.json()["code"] == "invalid_request"
        assert pinned.json()["errors"][0]["loc"] == ["expected_revision"]
        resumed = _act(api, headers, entry["id"], "repository_resume", key="p1")
        assert resumed.status_code == 200, resumed.text
        body = resumed.json()
        assert body["result"]["repository"]["id"] == entry["repository_id"]
        assert body["result"]["repository"]["health"]["state"] == "ok"
        assert body["result"]["operation"]["action"] == "repo.resume"
        assert body["operation_id"] == body["result"]["operation"]["id"]
        assert body["still_waiting"] is False
        replay = _act(api, headers, entry["id"], "repository_resume", key="p1")
        assert replay.status_code == 200, replay.text
        assert replay.json()["operation_id"] == body["operation_id"]
        assert replay.json()["replayed"] is True
        assert len(_recorded(api, "repo.resume")) == 1
        member = api.bearer(ROLE_CAPABILITIES["member"])
        api.loop.source = ResumableSource()
        refused = _act(api, member, entry["id"], "repository_resume", key="p2")
        assert refused.status_code == 403 and refused.json()["capability"] == "daemon:manage"


class TestAnEpicTask:
    def test_a_failed_task_is_retried_through_the_run(self, api: Api) -> None:
        plan, run, entry = _failed_task(api)
        headers = api.bearer()
        # The run's retry takes no revision, so the entry carries none.
        assert entry["revision"] is None
        pinned = _act(api, headers, entry["id"], "task_retry", key="t0", expected_revision=3)
        assert pinned.status_code == 422 and pinned.json()["code"] == "invalid_request"
        retried = _act(api, headers, entry["id"], "task_retry", key="t1")
        assert retried.status_code == 200, retried.text
        body = retried.json()
        assert body["result"]["id"] == run["id"] and body["result"]["replayed"] is False
        assert _task(body["result"], "A")["state"] in ("queued", "running")
        assert body["operation_id"] == body["result"]["operation_id"]
        assert body["still_waiting"] is False
        (recorded,) = _recorded(api, "plan.run.retry")
        assert recorded["id"] == body["operation_id"]
        assert recorded["request"] == {"plan_id": plan["id"], "node_id": entry["node_id"]}
        # The entry is gone; the same key still answers what happened.
        replay = _act(api, headers, entry["id"], "task_retry", key="t1")
        assert replay.status_code == 200, replay.text
        assert replay.json()["operation_id"] == body["operation_id"]
        assert replay.json()["replayed"] is True and replay.json()["result"]["replayed"] is True
        assert len(_recorded(api, "plan.run.retry")) == 1

    def test_a_failed_task_is_skipped_through_the_run(self, api: Api) -> None:
        plan, run, entry = _failed_task(api)
        headers = api.bearer()
        skipped = _act(api, headers, entry["id"], "task_skip", key="s1")
        assert skipped.status_code == 200, skipped.text
        body = skipped.json()
        assert _task(body["result"], "A")["state"] == "skipped"
        assert body["still_waiting"] is False
        (recorded,) = _recorded(api, "plan.run.skip")
        assert recorded["id"] == body["operation_id"]
        read = api.client.get(
            f"/v1/plans/{plan['id']}/nodes/{plan['root_id']}/run", headers=api.bearer(READ)
        ).json()
        assert _task(read, "A")["state"] == "skipped" and read["id"] == run["id"]

    def test_a_task_needs_the_runs_capability(self, api: Api) -> None:
        _, _, entry = _failed_task(api)
        reader = api.bearer(frozenset({"runs:read", "runs:control"}))
        refused = _act(api, reader, entry["id"], "task_skip")
        assert refused.status_code == 403 and refused.json()["capability"] == "plans:publish"
        assert _recorded(api, "plan.run.skip") == []


class TestRefusals:
    def test_an_entry_that_is_no_longer_waiting_says_so(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        assert _act(api, headers, entry["id"], "retry", key="a").status_code == 200
        # Never a silent success: the thing the person saw has been settled.
        gone = _act(api, headers, entry["id"], "retry", key="b")
        assert gone.status_code == 409, gone.text
        assert gone.json()["code"] == "not_waiting" and gone.json()["entry_id"] == entry["id"]
        unknown = _act(api, headers, "item:itm_nope:blocked", "retry", key="c")
        assert unknown.status_code == 409 and unknown.json()["code"] == "not_waiting"
        odd = _act(api, headers, "nonsense", "retry", key="d")
        assert odd.status_code == 409 and odd.json()["code"] == "not_waiting"
        assert len(_recorded(api, "item.retry")) == 1

    def test_an_action_the_entry_does_not_offer_is_named_with_what_it_does(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        offered = [a["action"] for a in entry["actions"]]
        assert "requeue" not in offered
        refused = _act(api, headers, entry["id"], "requeue")
        assert refused.status_code == 409, refused.text
        problem = refused.json()
        assert problem["code"] == "not_eligible" and problem["action"] == "requeue"
        assert problem["offered"] == offered and "requeue" in problem["detail"]
        # A task's action on an item, and a word that is no action at all.
        other = _act(api, headers, entry["id"], "task_retry", key="k2")
        assert other.status_code == 409 and other.json()["offered"] == offered
        unknown = _act(api, headers, entry["id"], "frobnicate", key="k3")
        assert unknown.status_code == 422 and unknown.json()["code"] == "unknown_action"
        assert [op for op in _operations(api) if op["action"].startswith("item.")] == []

    def test_a_caller_without_the_capability_is_refused_by_name(self, api: Api) -> None:
        _blocked(api)
        reader = api.bearer(frozenset({"runs:read"}))
        (entry,) = _entries(api, reader)
        refused = _act(api, reader, entry["id"], "retry")
        assert refused.status_code == 403, refused.text
        assert refused.json()["code"] == "forbidden"
        assert refused.json()["capability"] == "runs:control"
        # The list itself is the route's own capability.
        blind = _act(api, api.bearer(frozenset({"runs:control"})), entry["id"], "retry")
        assert blind.status_code == 403 and blind.json()["capability"] == "runs:read"
        anonymous = api.client.post(f"/v1/attention/{entry['id']}/act", json={"action": "retry"})
        assert anonymous.status_code == 401
        assert _recorded(api, "item.retry") == []

    def test_an_idempotency_key_is_required(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        refused = _act(api, headers, entry["id"], "retry", key=None)
        assert refused.status_code == 422
        assert refused.json()["code"] == "idempotency_key_required"
        assert _recorded(api, "item.retry") == []

    def test_the_revision_rides_the_request_and_not_the_params(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        refused = _act(
            api, headers, entry["id"], "retry", params={"expected_revision": entry["revision"]}
        )
        assert refused.status_code == 422 and refused.json()["code"] == "invalid_request"
        assert _recorded(api, "item.retry") == []


class TestIdempotency:
    def test_a_replay_answers_the_first_act_and_records_nothing_new(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        first = _act(api, headers, entry["id"], "retry", expected_revision=entry["revision"])
        assert first.status_code == 200, first.text
        assert first.json()["replayed"] is False
        # The entry is gone by now; the key still answers for the first act.
        replay = _act(api, headers, entry["id"], "retry", expected_revision=entry["revision"])
        assert replay.status_code == 200, replay.text
        assert replay.json()["operation_id"] == first.json()["operation_id"]
        assert replay.json()["replayed"] is True and replay.json()["still_waiting"] is False
        assert replay.json()["result"]["item"]["id"] == entry["item_id"]
        assert len(_recorded(api, "item.retry")) == 1

    def test_a_replayed_refusal_is_the_refusal_it_recorded(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        stale = _act(api, headers, entry["id"], "retry", expected_revision=entry["revision"] + 1)
        again = _act(api, headers, entry["id"], "retry", expected_revision=entry["revision"] + 1)
        assert stale.status_code == again.status_code == 409
        assert stale.json()["code"] == again.json()["code"] == "stale_revision"
        assert again.json()["operation_id"] == stale.json()["operation_id"]
        assert len(_recorded(api, "item.retry")) == 1

    def test_one_key_is_one_act(self, api: Api) -> None:
        _blocked(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        first = _act(api, headers, entry["id"], "dismiss", params={"reason": "known flake"})
        assert first.status_code == 200, first.text
        # Another action, or the same one with other arguments, under the
        # key that already named an act.
        other = _act(api, headers, entry["id"], "retry")
        assert other.status_code == 409, other.text
        assert other.json()["code"] == "idempotency_conflict"
        assert other.json()["operation_id"] == first.json()["operation_id"]
        changed = _act(api, headers, entry["id"], "dismiss", params={"reason": "something else"})
        assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
        assert _recorded(api, "item.retry") == [] and len(_recorded(api, "item.dismiss")) == 1
        # The key is the caller's own, on this entry.
        someone = _act(api, api.bearer(), entry["id"], "retry")
        assert someone.status_code == 200, someone.text

    def test_a_gate_approval_replays_too(self, api: Api) -> None:
        run_id = gated(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        first = _act(api, headers, entry["id"], "gate_approve", expected_revision=entry["revision"])
        replay = _act(
            api, headers, entry["id"], "gate_approve", expected_revision=entry["revision"]
        )
        assert first.status_code == 202 and replay.status_code == 202, replay.text
        assert replay.json()["operation_id"] == first.json()["operation_id"]
        assert replay.json()["replayed"] is True
        assert len(_recorded(api, "gate.approve")) == 1
        landed(api, run_id)


def test_an_act_costs_the_same_however_much_else_is_waiting(api: Api) -> None:
    """Finding the entry reads the list, in a fixed number of statements:
    acting on one alert must not cost more as the others pile up."""
    headers = api.bearer()
    for key in ("1", "2"):
        _parked(api, key)
    first = _entries(api, headers)[0]  # mints the public ids
    with statements(api) as seen:
        assert _act(api, headers, first["id"], "dismiss", key="a").status_code == 200
        few = len(seen)
    for key in ("3", "4", "5", "6", "7"):
        _parked(api, key)
    second = _entries(api, headers)[0]
    assert second["id"] != first["id"]
    with statements(api) as seen:
        assert _act(api, headers, second["id"], "dismiss", key="b").status_code == 200
        many = len(seen)
    assert few == many, f"{few} statements with two alerts, {many} with seven"


def test_every_act_is_one_operation(api: Api) -> None:
    """The act is routing: the command records its operation, and nothing
    is recorded for the act itself."""
    _blocked(api)
    headers = api.bearer()
    (entry,) = _entries(api, headers)
    before = len(_operations(api))
    dismissed = _act(api, headers, entry["id"], "dismiss", key="a")
    undone = _act(api, headers, entry["id"], "undismiss", key="b")
    retried = _act(api, headers, entry["id"], "retry", key="c")
    _act(api, headers, entry["id"], "retry", key="c")
    acts = [dismissed.json(), undone.json(), retried.json()]
    fresh = _operations(api)[: len(_operations(api)) - before]
    assert sorted(op["action"] for op in fresh) == ["item.dismiss", "item.retry", "item.undismiss"]
    assert {op["id"] for op in fresh} == {act["operation_id"] for act in acts}
    for act in acts:
        one = api.client.get(f"/v1/operations/{act['operation_id']}", headers=headers)
        assert one.status_code == 200 and one.json()["state"] == "succeeded"


def test_every_action_an_entry_can_offer_is_routed() -> None:
    """An action the list learns to advertise must be one the act can
    take, recorded under the operation its command records."""
    offered = {*ACTION_ORDER, *TASK_ACTIONS, *REPOSITORY_ACTIONS}
    assert set(attention_act.OPERATIONS) == offered
    assert set(attention_act.OPERATIONS.values()) <= set(EFFECTS)


@pytest.mark.parametrize("path", ["/v1/attention/{entry_id}/act"])
def test_the_act_is_in_the_published_contract(api: Api, path: str) -> None:
    spec = api.client.get("/v1/openapi.json").json()
    post = spec["paths"][path]["post"]
    schema = post["requestBody"]["content"]["application/json"]["schema"]
    assert schema["$ref"].endswith("/AttentionActRequest")
    assert {"403", "409", "422"} <= set(post["responses"])
    features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
    assert "attention.act" in features
