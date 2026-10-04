"""Triage: the operator agent retrying failures and granting rounds under an
owner's grants, on the real loop (the tests' ``Harness``: real stores, a
scripted runner) — and, for an epic run's task, over the recording forge.

Each failure is judged once per situation, written to the decisions ledger,
and acted on as ``agent:operator`` through a recorded operation; anything a
grant does not cover is one escalation a person's later act resolves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lantern.config import Config
from lantern.daemon.controls.delegation import (
    FAILURE_CAUSES,
    GRANTABLE_CAUSES,
    Grant,
    GrantInvalid,
    parse_conditions,
)
from lantern.daemon.controls.delegation_store import DecisionRecord
from lantern.daemon.triage import (
    GRANT_ROUNDS,
    MAX_ACTS,
    FailureFacts,
    Triage,
    classify,
)
from lantern.engine.model import RunResult
from tests.unit.test_daemon_loop import Harness, gh_item
from tests.unit.test_epic_runs import (
    ACTOR,
    _harness as _epic_harness,
    _issues,
    _node,
    _plan,
    _start,
    _states,
)

CI_TIMEOUT = "landing did not settle within ci_timeout_s=1800s"
CONFLICT = "the pull request conflicts with its base branch"


def _harness(tmp_path: Path, **daemon: Any) -> Harness:
    cfg = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "daemon": {"max_attempts_per_item": 1, "max_consecutive_failures": 50, **daemon},
            "landing": {"retry_rounds": 0},
        }
    )
    return Harness(tmp_path, cfg)


def _reasons(h: Harness, *reasons: str) -> None:
    """Each failed run in turn ends with the next of ``reasons``."""
    queue = list(reasons)
    inner = h.loop._runner

    def runner(item: Any, cfg: Config, run_id: str, bus: Any, resume: bool) -> RunResult:
        result: RunResult = inner(item, cfg, run_id, bus, resume)
        if result.state == "failed" and result.reason is None and queue:
            reason = queue.pop(0)
            h.store.set_run_reason(run_id, reason)
            return result.model_copy(update={"reason": reason})
        return result

    h.loop._runner = runner


def _grant(
    h: Harness,
    action: str = "item.retry",
    *,
    agent: str = "operator",
    daily_limit: int | None = None,
    **conditions: Any,
) -> Grant:
    return h.loop.delegation.create_grant(
        agent_slug=agent,
        action=action,
        conditions=parse_conditions(action, conditions),
        daily_limit=daily_limit,
        enabled=True,
        note=None,
        created_by="u1",
        created_by_display="Owner",
        now=h.clock(),
    )


def _fail(h: Harness, key: str = "1", outcome: str = "failed", reason: str = CI_TIMEOUT) -> str:
    """Queue item ``key`` and run it to a failure."""
    h.source.items = [gh_item(key)]
    h.outcomes = [outcome]
    if outcome == "failed":
        _reasons(h, reason)
    h.loop.tick()
    h.source.items = []
    item = h.dstore.get(f"gh:issue:{key}")
    assert item is not None and item.state in ("failed", "blocked"), item
    return item.item_id


def _decisions(h: Harness) -> list[DecisionRecord]:
    return list(reversed(h.loop.delegation.page(limit=100)))


def _triage(h: Harness) -> None:
    h.clock.t += 1
    h.loop.triage.tick(h.clock())


class TestClassify:
    @pytest.mark.parametrize(
        ("facts", "cause"),
        [
            (FailureFacts(run_state="provider_held", reason="usage limit"), "provider_throttle"),
            (FailureFacts(run_state="failed", exhausted="review"), "review_rounds_exhausted"),
            (FailureFacts(run_state="failed", exhausted="ci"), "ci_rounds_exhausted"),
            (FailureFacts(run_state="failed", verify_failed=True, reason="x"), "verify_failed"),
            (FailureFacts(run_state="blocked", reason=CI_TIMEOUT), "ci_timeout"),
            (
                FailureFacts(
                    run_state="blocked",
                    reason="CI did not report within ci_timeout_s=60s: check build needs a "
                    "maintainer to approve the workflow run",
                ),
                "needs_person",
            ),
            (
                FailureFacts(
                    run_state="blocked",
                    reason="a reviewer's changes-requested review is still standing after 2 "
                    "replied objection(s); only they can dismiss it",
                ),
                "needs_person",
            ),
            (
                FailureFacts(reason="nothing to deliver: the tree has no changes"),
                "needs_person",
            ),
            (FailureFacts(reason="GitHub refused: missing permission"), "needs_person"),
            (FailureFacts(run_state="failed", reason=CONFLICT), "merge_conflict"),
            (
                FailureFacts(reason="sandbox disk exhausted: 97% of the workspace is used"),
                "sandbox_resource",
            ),
            (FailureFacts(reason="the model provider is overloaded (429)"), "provider_throttle"),
            (FailureFacts(reason="GitHub API answered 502 Bad Gateway"), "forge_transient"),
            (FailureFacts(reason="connection reset talking to api.github.com"), "forge_transient"),
            (FailureFacts(reason="verify command failed: make test"), "verify_failed"),
            (FailureFacts(reason="a worker timed out"), "unknown"),
            (FailureFacts(run_state="blocked", reason="something odd"), "unknown"),
            (FailureFacts(run_state="failed"), "unknown"),
            (FailureFacts(), "unknown"),
        ],
    )
    def test_the_cause(self, facts: FailureFacts, cause: str) -> None:
        assert classify(facts) == cause
        assert cause in FAILURE_CAUSES

    def test_a_grant_lists_only_the_causes_triage_names(self) -> None:
        conditions = parse_conditions("item.retry", {"causes": list(GRANTABLE_CAUSES)})
        assert conditions.causes == GRANTABLE_CAUSES
        for cause in ("needs_person", "Needs_Person", "timeout", "unknown"):
            with pytest.raises(GrantInvalid) as refused:
                parse_conditions("item.retry", {"causes": [cause]})
            assert refused.value.field == "conditions.causes"


class TestNothingDelegated:
    def test_no_operator_grant_reads_and_writes_nothing(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        # A grant for another agent, and a disabled operator grant: neither counts.
        _grant(h, agent="critic")
        disabled = _grant(h)
        h.loop.delegation.update_grant(
            disabled.id, {"enabled": False}, expected_revision=1, now=h.clock()
        )
        before = h.dstore.get(item_id)
        for _ in range(3):
            h.clock.t += 5
            h.loop.tick()
        assert _decisions(h) == []
        assert h.loop.operations.page() == []
        assert h.dstore.get(item_id) == before

    def test_a_paused_daemon_takes_no_act(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        _grant(h, causes=["ci_timeout"])
        h.loop.pause(by="Ada")
        h.clock.t += 5
        h.loop.tick()
        assert _decisions(h) == []
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]


class TestActs:
    def test_item_retry_as_the_operator(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        grant = _grant(h, causes=["ci_timeout"], max_retries=2)
        _triage(h)
        item = h.dstore.get(item_id)
        assert item is not None and item.state == "queued" and item.attempts == 0
        (decision,) = _decisions(h)
        assert decision.outcome == "allow" and decision.grant_id == grant.id
        assert decision.action == "item.retry" and decision.item_id == item_id
        assert decision.repository == "o/r"
        assert decision.attrs["failure_cause"] == "ci_timeout"
        assert decision.attrs["retries"] == 0
        (op,) = h.loop.operations.page()
        assert op.id == decision.operation_id
        assert (op.action, op.target_kind, op.target_key) == ("item.retry", "item", item_id)
        assert op.state == "succeeded"
        assert op.actor["kind"] == "agent" and op.actor["id"] == "agent:operator"
        # The item says who asked.
        assert item.last_error == "re-queued by the operator agent"

    def test_grant_rounds_on_an_exhausted_run(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h, outcome="exhausted")
        item = h.dstore.get(item_id)
        assert item is not None and item.run_id is not None
        _grant(h, "run.grant_rounds", causes=["review_rounds_exhausted"])
        # An item.retry grant does not take an exhausted run: grant-rounds does.
        _grant(h, causes=["review_rounds_exhausted"])
        _triage(h)
        (decision,) = _decisions(h)
        assert decision.action == "run.grant_rounds" and decision.outcome == "allow"
        assert decision.run_id == item.run_id and decision.item_id == item_id
        assert decision.attrs["failure_cause"] == "review_rounds_exhausted"
        assert h.store.get_run(item.run_id).granted_rounds == GRANT_ROUNDS
        fresh = h.dstore.get(item_id)
        assert fresh is not None and fresh.state == "queued" and fresh.run_id == item.run_id
        (op,) = h.loop.operations.page()
        assert op.action == "run.grant_rounds" and op.request == {"rounds": GRANT_ROUNDS}
        assert op.actor["id"] == "agent:operator"

    def test_an_epic_task_is_retried_through_its_epic_run(self, tmp_path: Path) -> None:
        ops = _issues(11)
        h = _epic_harness(
            tmp_path, ops, daemon={"max_attempts_per_item": 1}, landing={"retry_rounds": 0}
        )
        _plan(h, _node("a", 11))
        h.outcomes = ["failed"]
        _reasons(h, CI_TIMEOUT)
        run = _start(h)
        for _ in range(3):
            h.loop.epic_runs.tick(h.clock())
            h.loop.tick()
        assert _states(h, run) == {"a": "failed"}
        _grant(h, "plan.run.retry", causes=["ci_timeout"])
        _grant(h, causes=["ci_timeout"])  # never used on an epic run's task
        _triage(h)
        (decision,) = _decisions(h)
        assert decision.action == "plan.run.retry" and decision.outcome == "allow"
        assert (decision.plan_id, decision.node_id, decision.epic_run_id) == (
            "plan_1",
            "a",
            run.id,
        )
        assert _states(h, run) == {"a": "queued"}
        (op,) = h.loop.operations.page()
        assert op.action == "plan.run.retry" and op.actor["id"] == "agent:operator"
        assert ACTOR["kind"] != op.actor["kind"]

    def test_an_act_that_fails_is_one_escalation_and_not_tried_again(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        _grant(h, causes=["ci_timeout"])

        def refuse(*_: Any, **__: Any) -> Any:
            raise ValueError("the queue said no")

        monkeypatch.setattr(h.loop, "retry_item", refuse)
        for _ in range(3):
            _triage(h)
        (escalation,) = _decisions(h)
        assert escalation.outcome == "escalate" and escalation.unresolved
        assert "did not happen: the queue said no" in escalation.reason
        (op,) = h.loop.operations.page()
        assert op.state == "failed" and op.id == escalation.operation_id
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]


class TestEscalations:
    def test_needs_person_escalates_once_and_a_retry_resolves_it(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h, outcome="blocked")
        _grant(h)  # any cause at all: needs_person still goes to a person
        for _ in range(3):
            _triage(h)
        (escalation,) = _decisions(h)
        assert escalation.outcome == "escalate" and escalation.unresolved
        assert escalation.attrs["failure_cause"] == "needs_person"
        assert h.dstore.get(item_id).state == "blocked"  # type: ignore[union-attr]
        # A person retries it: the escalation is closed as acted.
        h.loop.retry_item(item_id, by="Ada")
        _triage(h)
        closed = h.loop.delegation.decision(escalation.id)
        assert closed is not None and closed.resolution == "acted"

    def test_a_cause_the_grant_does_not_cover_escalates_once(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        _grant(h, causes=["merge_conflict"])
        for _ in range(3):
            _triage(h)
        (escalation,) = _decisions(h)
        assert escalation.outcome == "escalate"
        assert "causes does not include ci_timeout" in escalation.reason
        # Triage is restart-safe: a fresh instance writes nothing more.
        Triage(h.loop).tick(h.clock() + 10)
        assert len(_decisions(h)) == 1
        # A person dismisses it: declined.
        h.loop.dismiss_work(item_id=item_id, actor=ACTOR)
        _triage(h)
        closed = h.loop.delegation.decision(escalation.id)
        assert closed is not None and closed.resolution == "declined"
        _triage(h)
        assert len(_decisions(h)) == 1

    def test_unknown_always_escalates(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        _fail(h, reason="something nobody has seen before")
        _grant(h)
        _triage(h)
        (escalation,) = _decisions(h)
        assert escalation.outcome == "escalate"
        assert escalation.attrs["failure_cause"] == "unknown"

    def test_max_retries_counts_the_ledger_not_the_attempts(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        _grant(h, causes=["ci_timeout"], max_retries=1)
        _triage(h)
        assert h.dstore.get(item_id).state == "queued"  # type: ignore[union-attr]
        # It runs again and fails the same way.
        h.outcomes = ["failed"]
        _reasons(h, CI_TIMEOUT)
        h.clock.t += 5
        h.loop._dispatch_pass(h.clock(), discovered=[])  # dispatch without a triage pass
        item = h.dstore.get(item_id)
        assert item is not None and item.state == "failed" and item.attempts == 1
        _triage(h)
        _triage(h)
        allowed, escalated = _decisions(h)
        assert allowed.outcome == "allow"
        assert escalated.outcome == "escalate" and escalated.attrs["retries"] == 1
        assert "max_retries is 1 and retries is 1" in escalated.reason
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]


class TestLimits:
    def test_a_dismissed_item_is_never_touched(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        h.loop.dismiss_work(item_id=item_id, actor=ACTOR)
        _grant(h, causes=["ci_timeout"])
        _triage(h)
        assert _decisions(h) == []
        assert h.dstore.get(item_id).state == "failed"  # type: ignore[union-attr]

    def test_the_daily_limit(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        first = _fail(h, "1")
        second = _fail(h, "2")
        _grant(h, causes=["ci_timeout"], daily_limit=1)
        _triage(h)
        _triage(h)
        states = {i: h.dstore.get(i).state for i in (first, second)}  # type: ignore[union-attr]
        assert sorted(states.values()) == ["failed", "queued"]
        outcomes = sorted(d.outcome for d in _decisions(h))
        assert outcomes == ["allow", "escalate"]
        (escalation,) = [d for d in _decisions(h) if d.outcome == "escalate"]
        assert "daily_limit of 1 is spent" in escalation.reason

    def test_at_most_max_acts_in_one_tick(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        items = [_fail(h, str(n)) for n in range(1, MAX_ACTS + 2)]
        _grant(h, causes=["ci_timeout"])
        _triage(h)
        queued = [i for i in items if h.dstore.get(i).state == "queued"]  # type: ignore[union-attr]
        assert len(queued) == MAX_ACTS
        assert len(_decisions(h)) == MAX_ACTS
        _triage(h)
        assert all(h.dstore.get(i).state == "queued" for i in items)  # type: ignore[union-attr]

    def test_the_loop_ticks_triage(self, tmp_path: Path) -> None:
        h = _harness(tmp_path)
        item_id = _fail(h)
        _grant(h, causes=["ci_timeout"])
        h.outcomes = ["merged"]
        h.clock.t += 5
        h.loop.tick()
        (decision,) = _decisions(h)
        assert decision.outcome == "allow"
        assert h.dstore.get(item_id).state == "done"  # type: ignore[union-attr]
