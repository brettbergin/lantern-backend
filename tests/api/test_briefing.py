"""The briefing: one answer to "what happened while I was away, what needs
me, and is there work lined up" (``GET /v1/briefing``).

Everything in it exists on its own route already — the outcomes, the
attention counts, the decisions ledger, the plans, the queue, the usage
pool, the grants — so each test holds the briefing against that route or
against the store it is read from: the briefing is a summary, never a
second opinion. Run records carry the wall clock (as in a live daemon), so
a test about a window sets the harness clock to it and then places each
run's end by hand.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Any

from lantern.api.models import rfc3339
from lantern.daemon.controls.delegation import Conditions, Decision
from lantern.daemon.controls.delegation_store import DelegationStore
from lantern.daemon.sources import GitHubIssueSource
from lantern_worker.protocol import Usage
from tests.api.conftest import Api, build
from tests.api.test_attention import _blocked, _parked
from tests.api.test_grants import _headers
from tests.api.test_plans_publish import PUBLISH, _epic_with_tasks, _forge, _node, _publish
from tests.api.test_plans_run import _run as _start_epic
from tests.api.test_role_grants import _register_member, _register_owner
from tests.fakes.rawdb import exec_raw
from tests.unit.test_daemon_loop import PR_URL, gh_item
from tests.unit.test_daemon_sources import LABELS

DAY = 86400.0
WEEK = 7 * DAY


def _briefing(api: Api, headers: dict[str, str] | None = None, **params: Any) -> dict[str, Any]:
    response = api.client.get("/v1/briefing", params=params, headers=headers or api.bearer())
    assert response.status_code == 200, response.text
    return dict(response.json())


def _wall_clock(api: Api) -> float:
    """Move the harness clock to the wall clock, so the runs the scripted
    runner writes (stamped with ``time.time()``) and the window the route
    computes from the daemon's clock agree. The time it now reads."""
    api.clock.t = time.time()
    return float(api.clock.t)


def _finished(api: Api, key: str, outcome: str = "merged", *, ended_at: float) -> str:
    """A run that ended ``outcome`` at ``ended_at``; its run id."""
    api.harness.source.items = [gh_item(key, repo="o/r")]
    api.harness.outcomes = [outcome]
    api.clock.t += 10
    api.loop.tick()
    run_id = str(api.harness.runs[-1][0])
    exec_raw(
        api.harness.store,
        "UPDATE runs SET created_at=?, updated_at=? WHERE run_id=?",
        (ended_at - 600.0, ended_at, run_id),
    )
    return run_id


def _as_kind(api: Api, run_id: str, kind: str) -> None:
    exec_raw(api.harness.store, "UPDATE runs SET kind=? WHERE run_id=?", (kind, run_id))


def _decide(
    api: Api,
    outcome: str,
    *,
    at: float,
    grant_id: str | None = None,
    agent: str = "critic",
    **refs: Any,
) -> str:
    store: DelegationStore = api.loop.delegation
    row = store.record(
        Decision(outcome=outcome, grant_id=grant_id, reason=f"{outcome} because"),  # type: ignore[arg-type]
        agent_slug=agent,
        action="plan.approve",
        attrs={"repository": "o/r", "level": "epic"},
        now=at,
        **refs,
    )
    return row.id


def _grant(api: Api, *, daily_limit: int | None, enabled: bool = True) -> str:
    store: DelegationStore = api.loop.delegation
    grant = store.create_grant(
        agent_slug="critic",
        action="plan.approve",
        conditions=Conditions(),
        daily_limit=daily_limit,
        enabled=enabled,
        note=None,
        created_by=None,
        created_by_display=None,
        now=api.clock(),
    )
    return grant.id


def _published_plan(api: Api) -> dict[str, Any]:
    """A lone epic with tasks A and B published to the fake forge, and the
    daemon's issue source over that forge (so an epic run can admit them)."""
    fake = _forge(api)
    api.loop.source = GitHubIssueSource(lambda: fake, "o/r", LABELS, host="db")  # type: ignore[arg-type]
    headers = api.bearer(PUBLISH)
    plan = _epic_with_tasks(api, headers)
    published = _publish(api, headers, plan)
    assert published.status_code == 200, published.text
    return dict(published.json()["plan"])


def _plan_with(api: Api, states: dict[str, str], *, archived: bool = False) -> str:
    """A plan whose root is a draft epic and whose tasks are in ``states``
    (title to node state); archived when asked. The plan id."""
    headers = api.bearer(PUBLISH)
    plan = _epic_with_tasks(api, headers)
    stored = api.ctx.plans.get(plan["id"])
    assert stored is not None
    upsert = []
    for title, state in states.items():
        node = stored.node(_node(plan, title)["id"])
        assert node is not None
        upsert.append(replace(node, state=state))  # type: ignore[arg-type]
    api.ctx.plans.store.apply(
        stored.id,
        expected_revision=stored.revision,
        now=api.clock(),
        upsert=upsert,
        archived=archived,
    )
    return str(stored.id)


class TestAnEmptyWorkspace:
    def test_everything_is_zero_and_what_cannot_be_said_is_null(self, api: Api) -> None:
        now = api.clock()
        body = _briefing(api)
        assert body["workspace_id"] == "local"
        assert body["since"] == rfc3339(now - DAY)
        assert body["until"] == body["observed_at"] == rfc3339(now)
        assert body["outcomes"] == {
            "landed": 0,
            "failed": 0,
            "cancelled": 0,
            "by_kind": [],
            "recent_landed": [],
        }
        assert body["waiting"] == {
            "total": 0,
            "decision": 0,
            "failed": 0,
            "paused": 0,
            "oldest_since": None,
        }
        assert body["decided"] == {
            "allow": 0,
            "deny": 0,
            "escalate": 0,
            "unresolved_escalations": 0,
            "recent": [],
        }
        assert body["supply"] == {
            "proposed": 0,
            "approved": 0,
            "ready_tasks": 0,
            "queued": 0,
            "running": 0,
            "parked": 0,
        }
        # No landed run in the trailing week: no rate, and no days — never
        # a division by zero dressed up as a number.
        assert body["runway"] == {"ready_tasks": 0, "landed_per_day": None, "days": None}
        assert body["budget"] == {
            "runs_today": 0,
            "max_runs_per_day": 12,
            "tokens_today": 0,
            "daily_token_budget": None,
            "resets_at": body["budget"]["resets_at"],
        }
        assert body["budget"]["resets_at"].endswith("Z")
        assert body["grants"] == {"enabled": 0, "at_limit": 0}


class TestOutcomes:
    def test_runs_are_counted_by_how_and_when_they_ended(self, api: Api) -> None:
        now = _wall_clock(api)
        landed = _finished(api, "1", ended_at=now - 3600)
        _finished(api, "2", "failed", ended_at=now - 7200)
        _finished(api, "3", "cancelled", ended_at=now - 60)
        delivered = _finished(api, "4", "merged", ended_at=now - 120)
        _as_kind(api, delivered, "workload")
        # Ended before the window opened: yesterday's news, however recently
        # it was asked for.
        _finished(api, "5", ended_at=now - 2 * DAY)
        # Began long before the window and landed inside it: what finished
        # while the person was away, not what began then.
        old_ask = _finished(api, "6", ended_at=now - 1800)
        exec_raw(
            api.harness.store,
            "UPDATE runs SET created_at=? WHERE run_id=?",
            (now - 5 * DAY, old_ask),
        )
        # A blocked run is not an outcome: it waits on a person.
        _finished(api, "7", "blocked", ended_at=now - 30)
        body = _briefing(api)
        outcomes = body["outcomes"]
        assert (outcomes["landed"], outcomes["failed"], outcomes["cancelled"]) == (3, 1, 1)
        assert outcomes["by_kind"] == [
            {"kind": "code", "landed": 2, "failed": 1, "cancelled": 1},
            {"kind": "workload", "landed": 1, "failed": 0, "cancelled": 0},
        ]
        assert [r["run_id"] for r in outcomes["recent_landed"]] == [
            f"run_{delivered}",
            f"run_{old_ask}",
            f"run_{landed}",
        ]
        # The blocked run waits on a person (the failed one's item was
        # requeued for its second attempt, so it waits on nobody yet).
        assert body["waiting"]["failed"] == 1
        # A narrower window keeps only what ended inside it.
        narrow = _briefing(api, since=now - 900)["outcomes"]
        assert (narrow["landed"], narrow["failed"], narrow["cancelled"]) == (1, 0, 1)
        # A wider one reaches the older landing.
        wide = _briefing(api, since=now - 3 * DAY)["outcomes"]
        assert wide["landed"] == 4

    def test_recent_landed_is_newest_first_capped_and_names_the_work(self, api: Api) -> None:
        now = _wall_clock(api)
        runs = [_finished(api, str(n), ended_at=now - 60 * n) for n in range(1, 13)]
        deleted = runs[2]
        gone = api.client.post(
            f"/v1/runs/run_{deleted}/delete",
            json={"reason": "noise"},
            headers={**api.bearer(), "Idempotency-Key": "d1"},
        )
        assert gone.status_code in (200, 202), gone.text
        body = _briefing(api)
        recent = body["outcomes"]["recent_landed"]
        assert len(recent) == 10
        # Newest first; the deleted run is put away, never listed — and
        # still counted, as the analytics count it.
        expected = [r for r in runs if r != deleted][:10]
        assert [r["run_id"] for r in recent] == [f"run_{r}" for r in expected]
        assert body["outcomes"]["landed"] == 12
        first = recent[0]
        assert first == {
            "run_id": f"run_{runs[0]}",
            "kind": "code",
            "title": "Do 1",
            "repository": "o/r",
            "pull_request_number": 9,
            "pull_request_url": PR_URL,
            "landed_at": rfc3339(now - 60),
        }
        assert (
            api.client.get(f"/v1/runs/{first['run_id']}", headers=api.bearer()).status_code == 200
        )


class TestWaiting:
    def test_the_counts_and_the_oldest_wait_are_the_attention_list_s(self, api: Api) -> None:
        _blocked(api, "1")
        _parked(api, "2", "failed")
        _parked(api, "3", "blocked")
        attention = api.client.get("/v1/attention", headers=api.bearer()).json()
        assert attention["counts"]["total"] == 3
        body = _briefing(api)
        waiting = body["waiting"]
        assert {k: waiting[k] for k in ("total", "decision", "failed", "paused")} == attention[
            "counts"
        ]
        assert waiting["oldest_since"] == min(e["since"] for e in attention["data"])
        # Parked work is supply too: it is lined up behind a person.
        assert body["supply"]["parked"] == waiting["decision"] + waiting["paused"]


class TestDecided:
    def _seed(self, api: Api) -> dict[str, str]:
        now = api.clock()
        ids = {
            "allowed": _decide(
                api,
                "allow",
                at=now - 60,
                grant_id="grant_a",
                plan_id="plan_1",
                node_id="node_1",
                run_id="r1234abcd",
                operation_id="op_1",
            ),
            "allowed_earlier": _decide(api, "allow", at=now - 3600, grant_id="grant_a"),
            "denied": _decide(api, "deny", at=now - 120),
            "escalated": _decide(api, "escalate", at=now - 180, agent="operator"),
            # Before the window: not what was decided while away, though
            # the person is still owed its answer.
            "old_escalation": _decide(api, "escalate", at=now - 3 * DAY),
            "old_allow": _decide(api, "allow", at=now - 2 * DAY, grant_id="grant_a"),
        }
        store: DelegationStore = api.loop.delegation
        store.resolve(ids["escalated"], by="usr_owner", resolution="declined", now=now - 30)
        return ids

    def test_decisions_are_counted_by_outcome_in_the_window(self, api: Api) -> None:
        self._seed(api)
        decided = _briefing(api)["decided"]
        assert (decided["allow"], decided["deny"], decided["escalate"]) == (2, 1, 1)
        # Every escalation nobody has answered, however old.
        assert decided["unresolved_escalations"] == 1

    def test_the_recent_allows_are_detailed_for_an_auditor_only(self, api: Api) -> None:
        ids = self._seed(api)
        owner = _headers(_register_owner(api))
        recent = _briefing(api, owner)["decided"]["recent"]
        assert [d["id"] for d in recent] == [ids["allowed"], ids["allowed_earlier"]]
        assert recent[0] == {
            "id": ids["allowed"],
            "grant_id": "grant_a",
            "agent_slug": "critic",
            "action": "plan.approve",
            "reason": "allow because",
            "at": rfc3339(api.clock() - 60),
            "plan_id": "plan_1",
            "node_id": "node_1",
            "item_id": None,
            "run_id": "run_r1234abcd",
            "epic_run_id": None,
            "repository": "o/r",
            "operation_id": "op_1",
        }
        # A member reads the counts and nothing of what was decided.
        member = _headers(_register_member(api))
        decided = _briefing(api, member)["decided"]
        assert decided["recent"] is None
        assert (decided["allow"], decided["deny"], decided["escalate"]) == (2, 1, 1)
        assert api.client.get("/v1/decisions", headers=member).status_code == 403

    def test_the_recent_allows_are_capped_at_ten(self, api: Api) -> None:
        now = api.clock()
        for n in range(12):
            _decide(api, "allow", at=now - n, grant_id="grant_a")
        decided = _briefing(api)["decided"]
        assert decided["allow"] == 12 and len(decided["recent"]) == 10
        ats = [d["at"] for d in decided["recent"]]
        assert ats == sorted(ats, reverse=True)


class TestSupply:
    def test_nodes_are_counted_by_state_across_plans_and_archived_ones_are_not(
        self, api: Api, monkeypatch: Any
    ) -> None:
        plan = _published_plan(api)
        _plan_with(api, {"A": "proposed", "B": "approved"})
        _plan_with(api, {"A": "proposed", "B": "proposed"}, archived=True)
        supply = _briefing(api)["supply"]
        # The published plan's A and B are on the forge, open, and no epic
        # run has admitted them; the second plan's A waits for approval and
        # its B for publication; the archived plan counts for nothing.
        assert supply == {
            "proposed": 1,
            "approved": 1,
            "ready_tasks": 2,
            "queued": 0,
            "running": 0,
            "parked": 0,
        }
        # Starting the epic admits A (B depends on it): A is queued, no
        # longer lined up; B still is.
        started = _start_epic(api, api.bearer(PUBLISH), plan)
        assert started.status_code == 201, started.text
        supply = _briefing(api)["supply"]
        assert (supply["ready_tasks"], supply["queued"]) == (1, 1)
        # A task whose issue closed on the forge is not lined up either.
        stored = api.ctx.plans.get(plan["id"])
        assert stored is not None
        b = stored.node(_node(plan, "B")["id"])
        assert b is not None and b.forge is not None
        api.ctx.plans.store.apply(
            stored.id,
            expected_revision=stored.revision,
            now=api.clock(),
            upsert=[replace(b, forge=replace(b.forge, state="closed"))],
        )
        assert _briefing(api)["supply"]["ready_tasks"] == 0
        # Runs in flight are what the live status says.
        original = api.loop.status
        monkeypatch.setattr(
            api.loop,
            "status",
            lambda: {**original(), "runs": [{"run_id": "r_live_1"}, {"run_id": "r_live_2"}]},
        )
        assert _briefing(api)["supply"]["running"] == 2


class TestRunway:
    def test_days_are_the_tasks_lined_up_over_the_week_s_landing_rate(self, api: Api) -> None:
        now = _wall_clock(api)
        _finished(api, "1", ended_at=now - 3600)
        # Three days back: outside today's window, inside the trailing week.
        _finished(api, "2", ended_at=now - 3 * DAY)
        # Not a code run, and a code run that landed eight days ago: neither
        # is the rate.
        _as_kind(api, _finished(api, "3", ended_at=now - 60), "workload")
        _finished(api, "4", ended_at=now - 8 * DAY)
        # A failure is not a landing.
        _finished(api, "5", "failed", ended_at=now - 120)
        _published_plan(api)
        runway = _briefing(api)["runway"]
        assert runway["ready_tasks"] == 2
        assert runway["landed_per_day"] == 2 / 7
        assert runway["days"] == 2 / (2 / 7)
        # The window asked for does not move the rate: it is the week's.
        assert _briefing(api, since=now - 300)["runway"] == runway

    def test_without_a_landing_in_the_week_there_is_no_rate(self, api: Api) -> None:
        now = _wall_clock(api)
        _finished(api, "1", ended_at=now - 8 * DAY)
        _published_plan(api)
        assert _briefing(api)["runway"] == {
            "ready_tasks": 2,
            "landed_per_day": None,
            "days": None,
        }


class TestBudget:
    def test_today_s_runs_and_tokens_stand_against_the_cap_and_the_budget(self, api: Api) -> None:
        now = _wall_clock(api)
        _finished(api, "1", ended_at=now - 60)
        api.loop.usage_pool.charge(
            source="run",
            ref_id="r1",
            agent_slug=None,
            channel_id=None,
            usage=Usage(input_tokens=300, output_tokens=50, cache_read_tokens=1000),
        )
        pool = api.client.get("/v1/usage/pool", headers=api.bearer()).json()
        budget = _briefing(api)["budget"]
        assert budget == {
            "runs_today": pool["runs_today"],
            "max_runs_per_day": pool["max_runs_per_day"],
            "tokens_today": pool["tokens_today"],
            "daily_token_budget": None,
            "resets_at": pool["resets_at"],
        }
        assert budget["runs_today"] == 1 and budget["tokens_today"] == 350

    def test_a_configured_token_budget_is_named(self, tmp_path: Any) -> None:
        api = build(tmp_path, config={"daemon": {"daily_token_budget": 5000}})
        with api.client:
            budget = _briefing(api)["budget"]
        api.ctx.close()
        assert budget["daily_token_budget"] == 5000 and budget["tokens_today"] == 0


class TestGrants:
    def test_enabled_grants_are_counted_with_those_that_spent_today_s_limit(self, api: Api) -> None:
        now = api.clock()
        spent = _grant(api, daily_limit=2)
        _decide(api, "allow", at=now - 60, grant_id=spent)
        _decide(api, "allow", at=now - 30, grant_id=spent)
        room = _grant(api, daily_limit=5)
        _decide(api, "allow", at=now - 10, grant_id=room)
        _grant(api, daily_limit=None)
        _grant(api, daily_limit=1, enabled=False)
        grants = _briefing(api)["grants"]
        assert grants == {"enabled": 3, "at_limit": 1}
        listed = api.client.get("/v1/grants", headers=api.bearer()).json()["data"]
        used = {g["id"]: g["used_today"] for g in listed}
        assert used[spent] == 2 and used[room] == 1


class TestTheContract:
    def test_a_bad_since_is_refused(self, api: Api) -> None:
        now = _wall_clock(api)
        headers = api.bearer()
        for since in ("yesterday", "nan", now + 60, now - 91 * DAY, -1):
            refused = api.client.get("/v1/briefing", params={"since": since}, headers=headers)
            assert refused.status_code == 422, (since, refused.text)
            assert refused.json()["code"] == "invalid_request", since
        # Just inside the bound is fine: an RFC 3339 time ninety days back
        # (a second short of it, as a timestamp round-trips through text).
        edge = rfc3339(now - 90 * DAY + 1)
        assert _briefing(api, since=edge)["since"] == edge

    def test_reading_it_needs_runs_read(self, api: Api) -> None:
        headers = api.bearer(frozenset({"collaboration:read"}))
        assert api.client.get("/v1/briefing", headers=headers).status_code == 403
        assert api.client.get("/v1/briefing").status_code == 401

    def test_the_feature_is_advertised(self, api: Api) -> None:
        features = api.client.get("/v1/capabilities", headers=api.bearer()).json()["features"]
        assert "briefing" in features
