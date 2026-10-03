"""Every mutating control leaves a durable record before it acts, and a
process that comes back settles what a dead one left from evidence."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from lantern.config import Config
from lantern.daemon.control import ControlClient, ControlServer, dispatch
from lantern.daemon.controls import ControlError, ControlService, Principal
from lantern.daemon.controls.delegation import Conditions, Grant
from lantern.daemon.controls.generation import GENERATION_KEY
from lantern.daemon.controls.operations import (
    IdempotencyConflict,
    OperationReplay,
    OperationRunner,
    OperationSpec,
    OperationStore,
    reconcile_operations,
    record_plan_operation,
)
from lantern.daemon.controls.results import PauseOutcome
from lantern.daemon.model import WorkItem
from lantern.daemon.store import DaemonStore
from lantern.engine.model import RunResult
from lantern.events import EventBus
from lantern.paths import LanternHome
from lantern.plans import PlanRefusal
from tests.unit.test_daemon_discord import FakeLoop
from tests.unit.test_daemon_loop import Harness, gh_item

OPS = Principal.trusted("ops via test", "ctl")
CRITIC = Principal.for_agent("critic")
PERSON = {"kind": "person", "id": "p1", "display": "Pat"}


class RecordingLoop(FakeLoop):
    """FakeLoop with an operation store, the way the real loop has one."""

    def __init__(self, dstore: DaemonStore) -> None:
        super().__init__(dstore)
        self.operations = OperationStore(dstore)
        self.generation = "g_test"
        self.now = 100.0

    def clock(self) -> float:
        return self.now


@pytest.fixture
def floop(tmp_path: Path) -> RecordingLoop:
    return RecordingLoop(DaemonStore(LanternHome(tmp_path).state_db))


def spec(**overrides: Any) -> OperationSpec:
    fields: dict[str, Any] = {
        "action": "daemon.pause",
        "target_kind": "hold",
        "target_key": "operator",
        "principal": OPS,
        "request": {"hold": "operator"},
    }
    fields.update(overrides)
    return OperationSpec(**fields)


def plan_spec(**overrides: Any) -> OperationSpec:
    fields: dict[str, Any] = {
        "action": "plan.approve",
        "target_kind": "plan",
        "target_key": "plan_1",
        "principal": CRITIC,
        "request": {"plan_id": "plan_1", "node_id": "node_1"},
    }
    fields.update(overrides)
    return OperationSpec(**fields)


def _grant(h: Harness) -> Grant:
    """A stored grant at revision 1, as an owner's write leaves it."""
    grant: Grant = h.loop.delegation.create_grant(
        agent_slug="critic",
        action="plan.approve",
        conditions=Conditions(max_children=8),
        daily_limit=5,
        enabled=True,
        note="small levels",
        created_by="usr_owner",
        created_by_display="owner",
        now=1.0,
    )
    return grant


class TestStore:
    def test_accept_writes_the_row_and_its_event_together(self, floop: RecordingLoop) -> None:
        op, created = floop.operations.accept(spec(), now=1.0)
        assert created and op.state == "accepted" and op.id.startswith("op_")
        assert op.effect == "the hold stands and nothing new is claimed"
        assert op.actor == OPS.audit() and op.request == {"hold": "operator"}
        (event,) = floop.operations.events()
        assert event["type"] == "operation.accepted" and event["operation_id"] == op.id
        assert event["actor"] == OPS.audit() and event["data"]["action"] == "daemon.pause"
        assert event["seq"] == 1 and event["source_seq"] is None

    def test_same_key_same_request_returns_the_same_operation(self, floop: RecordingLoop) -> None:
        first, created = floop.operations.accept(spec(idempotency=("c1", "k1")), now=1.0)
        again, created_again = floop.operations.accept(spec(idempotency=("c1", "k1")), now=2.0)
        assert created and not created_again and again.id == first.id
        # Another scope's key is another operation.
        other, _ = floop.operations.accept(spec(idempotency=("c2", "k1")), now=3.0)
        assert other.id != first.id
        assert len(floop.operations.recent()) == 2

    def test_same_key_different_request_is_a_conflict(self, floop: RecordingLoop) -> None:
        first, _ = floop.operations.accept(spec(idempotency=("c1", "k1")), now=1.0)
        with pytest.raises(IdempotencyConflict) as excinfo:
            floop.operations.accept(
                spec(idempotency=("c1", "k1"), request={"hold": "deploy"}), now=2.0
            )
        assert excinfo.value.existing.id == first.id

    def test_the_first_verdict_stands(self, floop: RecordingLoop) -> None:
        op, _ = floop.operations.accept(spec(), now=1.0)
        floop.operations.claim(op.id, "g1", now=2.0)
        done = floop.operations.finish(op.id, 3.0, state="succeeded", result={"holds": ["x"]})
        assert done is not None and done.state == "succeeded" and done.result == {"holds": ["x"]}
        assert done.claimed_generation == "g1" and done.finished_at == 3.0
        later = floop.operations.finish(op.id, 4.0, state="failed", error_code="late")
        assert later is not None and later.state == "succeeded" and later.error_code is None
        types = [e["type"] for e in floop.operations.events()]
        assert types == ["operation.accepted", "operation.finished"]

    def test_reads_are_bounded_and_filtered(self, floop: RecordingLoop) -> None:
        for i in range(5):
            op, _ = floop.operations.accept(
                spec(target_key=f"h{i}", request={"i": i}), now=float(i)
            )
            if i % 2:
                floop.operations.finish(op.id, 10.0, state="failed", error_code="x")
        assert [o.target_key for o in floop.operations.recent(limit=2)] == ["h4", "h3"]
        assert {o.target_key for o in floop.operations.recent(states=["failed"])} == {"h1", "h3"}
        assert [o.target_key for o in floop.operations.recent(target=("hold", "h2"))] == ["h2"]
        assert [o.target_key for o in floop.operations.pending()] == ["h0", "h2", "h4"]
        assert [e["seq"] for e in floop.operations.events(after_seq=5)] == [6, 7]


class TestRunner:
    def runner(self, floop: RecordingLoop) -> OperationRunner:
        return OperationRunner(
            floop.operations, generation=lambda: floop.generation, clock=floop.clock
        )

    def test_accept_claim_apply_finish(self, floop: RecordingLoop) -> None:
        seen: list[str] = []
        runner = self.runner(floop)
        runner.after_accept = lambda op: seen.append(f"accepted:{op.state}")
        runner.after_claim = lambda op: seen.append("claimed")
        runner.after_effect = lambda op: seen.append("effect")
        runner.after_commit = lambda op: seen.append("committed")
        outcome = runner.run_sync(spec(), lambda op_id: PauseOutcome(hold="operator", holds=["x"]))
        assert outcome.operation_id is not None and outcome.holds == ["x"]
        assert seen == ["accepted:accepted", "claimed", "effect", "committed"]
        stored = floop.operations.get(outcome.operation_id)
        assert stored is not None and stored.state == "succeeded"
        assert stored.claimed_generation == "g_test"
        assert stored.result == {
            "operation_id": None,
            "hold": "operator",
            "holds": ["x"],
            "fresh": True,
            "reason": "",
        }

    def test_a_refusal_finishes_the_record_failed_and_is_re_raised(
        self, floop: RecordingLoop
    ) -> None:
        def refuse(op_id: str) -> PauseOutcome:
            raise ControlError("not_eligible", "nothing is running.")

        with pytest.raises(ControlError):
            self.runner(floop).run_sync(spec(), refuse)
        (op,) = floop.operations.recent()
        assert (op.state, op.error_code, op.error_detail) == (
            "failed",
            "not_eligible",
            "nothing is running.",
        )

    def test_a_crash_in_the_effect_is_recorded_not_hidden(self, floop: RecordingLoop) -> None:
        def explode(op_id: str) -> PauseOutcome:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            self.runner(floop).run_sync(spec(), explode)
        (op,) = floop.operations.recent()
        assert op.state == "failed" and op.error_code == "crashed"
        assert op.error_detail == "RuntimeError: boom"

    def test_a_replay_never_applies_the_effect_twice(self, floop: RecordingLoop) -> None:
        applied: list[str] = []
        runner = self.runner(floop)
        first = runner.run_sync(
            spec(idempotency=("c", "k")),
            lambda op_id: (applied.append(op_id), PauseOutcome(hold="o", holds=[]))[1],
        )
        with pytest.raises(OperationReplay) as excinfo:
            runner.run_sync(
                spec(idempotency=("c", "k")),
                lambda op_id: (applied.append(op_id), PauseOutcome(hold="o", holds=[]))[1],
            )
        assert excinfo.value.existing.id == first.operation_id and applied == [first.operation_id]

    def test_a_deferred_effect_leaves_the_record_running(self, floop: RecordingLoop) -> None:
        outcome = self.runner(floop).run_sync(
            spec(action="run.cancel", target_kind="run", target_key="r1", deferred=True),
            lambda op_id: PauseOutcome(hold="o", holds=[]),
        )
        assert outcome.operation_id is not None
        stored = floop.operations.get(outcome.operation_id)
        assert stored is not None and stored.state == "running"

    def test_a_deferred_after_finishes_when_the_effect_runs(self, floop: RecordingLoop) -> None:
        """A stop's flag is set once the reply is on its way; the record
        says succeeded only once it has been."""
        service = ControlService(floop)
        stop = service.stop(OPS)
        assert stop.operation_id is not None
        before = floop.operations.get(stop.operation_id)
        assert before is not None and before.state == "running"
        assert not getattr(floop, "stopped", False)
        stop.after()
        assert floop.stopped
        after = floop.operations.get(stop.operation_id)
        assert after is not None and after.state == "succeeded"
        assert service.operation_ids == [stop.operation_id]


class TestAPlanWriteRecordedByTheDaemon:
    """``record_plan_operation`` over a real store and nothing else: no API
    context, no HTTP — what a daemon-side driver calls."""

    def record(self, floop: RecordingLoop, call: Any, **overrides: Any) -> tuple[str, Any]:
        return record_plan_operation(
            floop.operations,
            plan_spec(**overrides),
            call=call,
            result=lambda value: {"revision": value},
            clock=floop.clock,
            generation=floop.generation,
        )

    def test_accept_claim_call_finish(self, floop: RecordingLoop) -> None:
        during: list[str] = []

        def call() -> int:
            (op,) = floop.operations.recent()
            during.append(op.state)
            return 4

        op_id, value = self.record(floop, call)
        assert value == 4 and during == ["running"]
        stored = floop.operations.get(op_id)
        assert stored is not None and stored.state == "succeeded"
        assert stored.result == {"revision": 4}
        assert stored.claimed_generation == "g_test" and stored.finished_at == 100.0
        assert stored.actor == CRITIC.audit() and stored.actor["kind"] == "agent"
        assert stored.effect == "the node's draft and proposed children are approved"
        events = floop.operations.events()
        assert [e["type"] for e in events] == ["operation.accepted", "operation.finished"]
        assert events[0]["actor"] == CRITIC.audit()
        assert {e["operation_id"] for e in events} == {op_id}

    def test_a_refusal_finishes_failed_with_its_status_and_names_the_record(
        self, floop: RecordingLoop
    ) -> None:
        def refuse() -> int:
            raise PlanRefusal(409, "stale_revision", "it is at revision 7", current_revision=7)

        with pytest.raises(PlanRefusal) as excinfo:
            self.record(floop, refuse)
        (op,) = floop.operations.recent()
        assert (op.state, op.error_code, op.error_detail) == (
            "failed",
            "stale_revision",
            "it is at revision 7",
        )
        # The record keeps the refusal as it was raised; the refusal that
        # travels on names its record.
        assert op.result == {"status": 409, "extra": {"current_revision": 7}}
        assert excinfo.value.extra == {"current_revision": 7, "operation_id": op.id}
        assert (excinfo.value.status, excinfo.value.code) == (409, "stale_revision")

    def test_a_crash_in_the_call_is_recorded_not_hidden(self, floop: RecordingLoop) -> None:
        def explode() -> int:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            self.record(floop, explode)
        (op,) = floop.operations.recent()
        assert (op.state, op.error_code, op.error_detail) == (
            "failed",
            "crashed",
            "RuntimeError: boom",
        )
        assert op.result is None

    def test_a_replay_never_makes_the_call_twice(self, floop: RecordingLoop) -> None:
        calls: list[int] = []

        def call() -> int:
            calls.append(1)
            return 4

        op_id, _ = self.record(floop, call, idempotency=("driver", "plan_1:node_1:3"))
        with pytest.raises(OperationReplay) as excinfo:
            self.record(floop, call, idempotency=("driver", "plan_1:node_1:3"))
        assert excinfo.value.existing.id == op_id and calls == [1]
        assert excinfo.value.existing.state == "succeeded"
        assert excinfo.value.existing.result == {"revision": 4}
        assert len(floop.operations.recent()) == 1

    def test_another_request_under_the_same_key_is_a_conflict(self, floop: RecordingLoop) -> None:
        calls: list[int] = []
        op_id, _ = self.record(floop, lambda: 4, idempotency=("driver", "k"))
        with pytest.raises(IdempotencyConflict) as excinfo:
            self.record(
                floop,
                lambda: calls.append(1),
                idempotency=("driver", "k"),
                request={"plan_id": "plan_1", "node_id": "node_2"},
            )
        assert excinfo.value.existing.id == op_id and calls == []

    def test_without_a_key_each_call_is_its_own_operation(self, floop: RecordingLoop) -> None:
        first, _ = self.record(floop, lambda: 4)
        second, _ = self.record(floop, lambda: 5)
        assert first != second and len(floop.operations.recent()) == 2


class TestEverySurfaceRecords:
    def test_ctl_pause_leaves_one_record_attributed_to_ctl(self, tmp_path: Path) -> None:
        home = LanternHome(tmp_path / ".lantern")
        floop = RecordingLoop(DaemonStore(home.state_db))
        server = ControlServer(floop, home, poll_s=0.02)
        server.start()
        try:
            reply = ControlClient(home, by="brett via ctl").submit("pause --hold deploy-1")
        finally:
            server.close()
        assert reply is not None and reply.ok and reply.operation_id is not None
        (op,) = floop.operations.recent()
        assert op.id == reply.operation_id and op.state == "succeeded"
        assert op.action == "daemon.pause" and op.target_key == "deploy-1"
        assert op.actor["via"] == "ctl" and op.actor["display"] == "brett via ctl"
        assert op.result == {
            "operation_id": None,
            "hold": "deploy-1",
            "holds": ["deploy-1"],
            "fresh": True,
            "reason": "",
        }

    def test_reads_leave_no_record(self, floop: RecordingLoop) -> None:
        for cmd in ("status", "queue", "items"):
            reply = dispatch(floop, cmd)
            assert reply.ok and reply.operation_id is None
        assert floop.operations.recent() == []

    def test_a_refused_verb_is_recorded_as_failed(self, floop: RecordingLoop) -> None:
        floop.dstore.upsert_new(WorkItem(item_id="gh:issue:8", source_key="8", title="8"), 1.0)
        floop.dstore.mark_running("gh:issue:8", "r1", 2.0)
        floop.dstore.mark_done("gh:issue:8", now=3.0)
        reply = dispatch(floop, "retry gh:issue:8", by="ops")
        assert not reply.ok and reply.text.startswith("retry failed:")
        # The record carries the refusal; the prose edge carried the sentence.
        (op,) = floop.operations.recent()
        assert op.state == "failed" and op.error_code == "not_eligible"
        assert reply.operation_id == op.id


class TestCancelRecord:
    """The cancel's record is finished by what the run actually did."""

    def _tick_with_cancel(
        self, h: Harness, *, honour: bool, by: str = "ops via test"
    ) -> tuple[str, str | None]:
        started = threading.Event()
        release = threading.Event()
        run_ids: list[str] = []

        def runner(
            item: WorkItem, cfg: Config, run_id: str, bus: EventBus, resume: bool
        ) -> RunResult:
            run_ids.append(run_id)
            started.set()
            release.wait(5)
            if honour:
                from lantern.errors import RunCancelledError

                h.store.create_run(run_id, "outcome")
                h.store.set_run_state(run_id, "building")
                raise RunCancelledError("cancelled")
            return h.runner(item, cfg, run_id, bus, resume)

        h.loop._runner = runner
        t = threading.Thread(target=h.loop.tick)
        t.start()
        assert started.wait(5)
        service = ControlService(h.loop)
        outcome = service.cancel_current(Principal.trusted(by, "ctl"))
        release.set()
        t.join(5)
        assert outcome.operation_id is not None
        assert outcome.target == run_ids[0]
        return outcome.operation_id, run_ids[0]

    def test_an_honoured_cancel_succeeds_when_the_run_settles(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        h.source.items = [gh_item()]
        op_id, run_id = self._tick_with_cancel(h, honour=True)
        op = h.loop.operations.get(op_id)
        assert op is not None and op.state == "succeeded" and op.target_key == run_id
        assert op.result == {"mode": "current", "retry": False, "run_id": run_id}
        assert h.dstore.get("gh:issue:1").state == "cancelled"  # type: ignore[union-attr]

    def test_a_late_cancel_reports_the_state_the_run_reached(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        h.source.items = [gh_item()]
        op_id, _ = self._tick_with_cancel(h, honour=False)
        op = h.loop.operations.get(op_id)
        assert op is not None and op.state == "failed"
        assert op.error_code == "target_already_terminal"
        assert op.error_detail is not None and "it is merged" in op.error_detail
        assert h.dstore.get("gh:issue:1").state == "done"  # type: ignore[union-attr]


class TestReconciler:
    """What a dead generation left, judged from evidence at recovery."""

    def test_recover_stamps_a_generation(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.loop.generation is None and h.loop.status()["generation"] is None
        h.loop.recover()
        assert h.loop.generation is not None
        assert h.dstore.get_value(GENERATION_KEY) == h.loop.generation
        assert h.loop.status()["generation"] == h.loop.generation

    def test_an_unclaimed_command_expires_rather_than_running_at_boot(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(spec(principal=OPS), now=1.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == "expired"
        assert settled.error_detail == "the daemon restarted before the command was claimed"

    def test_a_deadline_is_named_when_it_passed(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(spec(ttl_s=10.0), now=h.clock() - 100)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == "expired"
        assert settled.error_detail == "past its deadline before it was claimed"

    @pytest.mark.parametrize(
        ("run_state", "expected", "code"),
        [
            ("cancelled", "succeeded", None),
            ("merged", "failed", "target_already_terminal"),
            ("building", "failed", "interrupted_before_effect"),
        ],
    )
    def test_a_claimed_cancel_is_judged_from_the_run(
        self, tmp_path: Path, run_state: str, expected: str, code: str | None
    ) -> None:
        h = Harness(tmp_path)
        h.store.create_run("r1", "x")
        h.store.set_run_state("r1", run_state)  # type: ignore[arg-type]
        op, _ = h.loop.operations.accept(
            spec(action="run.cancel", target_kind="run", target_key="r1", deferred=True), now=1.0
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and (settled.state, settled.error_code) == (expected, code)

    def test_own_generation_claims_are_left_alone(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.loop.recover()
        assert h.loop.generation is not None
        op, _ = h.loop.operations.accept(
            spec(action="run.cancel", target_kind="run", target_key="r1"), now=1.0
        )
        h.loop.operations.claim(op.id, h.loop.generation, now=2.0)
        touched = reconcile_operations(h.loop, generation=h.loop.generation, now=3.0)
        assert touched == []
        assert h.loop.operations.get(op.id).state == "running"  # type: ignore[union-attr]

    def test_stop_and_restart_succeed_once_a_new_generation_answers(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for action in ("daemon.stop", "daemon.restart"):
            op, _ = h.loop.operations.accept(
                spec(action=action, target_kind="daemon", target_key="daemon"), now=1.0
            )
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert {o.state for o in h.loop.operations.recent()} == {"succeeded"}

    def test_item_verbs_are_judged_from_the_item(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_running("gh:issue:1", "r1", now=2.0)
        h.dstore.abandon("gh:issue:1", "gave up", now=3.0)
        done, _ = h.loop.operations.accept(
            spec(action="item.abandon", target_kind="item", target_key="gh:issue:1"), now=1.0
        )
        lost, _ = h.loop.operations.accept(
            spec(action="item.requeue", target_kind="item", target_key="gh:issue:1"), now=1.5
        )
        for op in (done, lost):
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert h.loop.operations.get(done.id).state == "succeeded"  # type: ignore[union-attr]
        judged = h.loop.operations.get(lost.id)
        assert judged is not None and judged.state == "failed"
        assert judged.error_code == "interrupted_before_effect" and judged.error_detail == (
            "item is failed"
        )

    def test_a_dismissal_is_judged_from_the_mark(self, tmp_path: Path) -> None:
        """The mark is the effect: a dismiss whose mark stands happened, an
        undismiss whose mark still stands did not — on the item for a run
        the item pins, as the verb writes it."""
        h = Harness(tmp_path)
        h.dstore.upsert_new(gh_item(), now=1.0)
        h.dstore.mark_running("gh:issue:1", "r1", now=2.0)
        h.dstore.mark_blocked("gh:issue:1", "needs a decision", now=3.0)
        h.dstore.set_work_mark("item", "gh:issue:1", "dismissed", cause="dismissed", at=4.0)
        ops = {
            name: h.loop.operations.accept(
                spec(action=action, target_kind=kind, target_key=key), now=1.0
            )[0]
            for name, action, kind, key in (
                ("item", "item.dismiss", "item", "gh:1"),
                ("pinned", "run.dismiss", "run", "r1"),
                ("undone", "item.undismiss", "item", "gh:issue:1"),
                ("orphan", "run.dismiss", "run", "r_gone"),
            )
        }
        for op in ops.values():
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        judged = {name: h.loop.operations.get(op.id) for name, op in ops.items()}
        assert judged["item"].state == judged["pinned"].state == "succeeded"  # type: ignore[union-attr]
        for name, detail in (
            ("undone", "the alert is still dismissed"),
            ("orphan", "the alert was not dismissed"),
        ):
            lost = judged[name]
            assert lost is not None and lost.state == "failed"
            assert lost.error_code == "interrupted_before_effect" and lost.error_detail == detail

    def test_a_delete_is_judged_from_the_mark_written_last(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for key in ("1", "2"):
            h.dstore.upsert_new(gh_item(key), now=1.0)
            h.dstore.mark_blocked(f"gh:issue:{key}", "needs a decision", now=2.0)
        h.dstore.mark_deleted("gh:issue:1", [], 3.0)
        done, _ = h.loop.operations.accept(
            spec(action="item.delete", target_kind="item", target_key="gh:1"), now=1.0
        )
        lost, _ = h.loop.operations.accept(
            spec(action="item.delete", target_kind="item", target_key="gh:issue:2"), now=1.0
        )
        for op in (done, lost):
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert h.loop.operations.get(done.id).state == "succeeded"  # type: ignore[union-attr]
        judged = h.loop.operations.get(lost.id)
        assert judged is not None and judged.state == "failed"
        assert judged.error_detail == "the delete was interrupted; sending it again finishes it"

    def test_an_interrupted_bulk_dismissal_says_it_is_safe_to_send_again(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(
            spec(action="attention.dismiss_all", target_kind="workspace", target_key="local"),
            now=1.0,
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        judged = h.loop.operations.get(op.id)
        assert judged is not None and judged.state == "failed"
        assert judged.error_code == "interrupted_before_effect"
        assert "sending it again dismisses what is left" in (judged.error_detail or "")

    @pytest.mark.parametrize(("opened_at", "expected"), [(None, "succeeded"), (5.0, "failed")])
    def test_a_claimed_breaker_reset_is_judged_from_the_breaker(
        self, tmp_path: Path, opened_at: float | None, expected: str
    ) -> None:
        h = Harness(tmp_path)
        h.dstore.set_breaker(opened_at, 3)
        op, _ = h.loop.operations.accept(
            spec(action="daemon.breaker_reset", target_kind="breaker", target_key="breaker"),
            now=1.0,
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == expected

    def _plan_with_children(self, h: Harness, *titles: str) -> tuple[str, str, list[str]]:
        """A draft epic with a draft task per title: the plan's id, its
        root's and the tasks'."""
        plans = h.loop.plans
        plan = plans.create(
            level="epic", repository="o/r", sections={"title": "An epic"}, now=1.0, actor=PERSON
        )
        children = []
        for title in titles:
            plan, node_id = plans.add_node(
                plan.id,
                expected_revision=plan.revision,
                parent_id=plan.root_id,
                repository=None,
                sections={"title": title},
                position=None,
                now=2.0,
                actor=PERSON,
            )
            children.append(node_id)
        return plan.id, plan.root_id, children

    def _claimed_approve(
        self, h: Harness, plan_id: str, node_id: str, node_ids: list[str] | None
    ) -> str:
        """An approve a dead generation claimed against the plan as it is."""
        plan = h.loop.plans.store.get(plan_id)
        revision = 1 if plan is None else plan.revision
        op, _ = h.loop.operations.accept(
            plan_spec(
                target_key=plan_id,
                request={
                    "plan_id": plan_id,
                    "node_id": node_id,
                    "expected_revision": revision,
                    "node_ids": node_ids,
                },
                expected_revision=revision,
            ),
            now=1.0,
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        return op.id

    @pytest.mark.parametrize("named", [False, True])
    def test_a_claimed_approve_that_landed_is_judged_succeeded(
        self, tmp_path: Path, named: bool
    ) -> None:
        h = Harness(tmp_path)
        plan_id, root, (a, _b) = self._plan_with_children(h, "A", "B")
        op_id = self._claimed_approve(h, plan_id, root, [a] if named else None)
        plan = h.loop.plans.get(plan_id)
        # The write the dead generation committed before it could say so.
        h.loop.plans.approve(
            plan_id,
            root,
            expected_revision=plan.revision,
            node_ids=[a] if named else None,
            now=3.0,
            actor=PERSON,
        )
        h.loop.recover()
        settled = h.loop.operations.get(op_id)
        assert settled is not None and (settled.state, settled.error_code) == ("succeeded", None)

    @pytest.mark.parametrize("named", [False, True])
    def test_a_claimed_approve_that_never_landed_is_failed_and_safe_to_repeat(
        self, tmp_path: Path, named: bool
    ) -> None:
        h = Harness(tmp_path)
        plan_id, root, (a, b) = self._plan_with_children(h, "A", "B")
        plan = h.loop.plans.get(plan_id)
        # Another child was approved since; the one this approve names is
        # where it was.
        h.loop.plans.approve(
            plan_id, root, expected_revision=plan.revision, node_ids=[a], now=3.0, actor=PERSON
        )
        op_id = self._claimed_approve(h, plan_id, root, [b] if named else None)
        # The plan moved on since, by a write that approved nothing.
        plan = h.loop.plans.get(plan_id)
        h.loop.plans.add_node(
            plan_id,
            expected_revision=plan.revision,
            parent_id=root,
            repository=None,
            sections={"title": "C"},
            position=None,
            now=4.0,
            actor=PERSON,
        )
        h.loop.recover()
        settled = h.loop.operations.get(op_id)
        assert settled is not None and settled.state == "failed"
        assert settled.error_code == "interrupted_before_effect"
        assert settled.error_detail == (
            "the approval was interrupted before it was written; approving the level again is safe"
        )

    def test_a_claimed_approve_is_never_succeeded_by_a_plan_nothing_wrote_to(
        self, tmp_path: Path
    ) -> None:
        """Children that were approved before the approve was even asked
        for are not evidence that it landed."""
        h = Harness(tmp_path)
        plan_id, root, (_a,) = self._plan_with_children(h, "A")
        plan = h.loop.plans.get(plan_id)
        h.loop.plans.approve(
            plan_id, root, expected_revision=plan.revision, node_ids=None, now=3.0, actor=PERSON
        )
        op_id = self._claimed_approve(h, plan_id, root, None)
        h.loop.recover()
        settled = h.loop.operations.get(op_id)
        assert settled is not None and settled.state == "failed"
        assert settled.error_code == "interrupted_before_effect"

    def test_the_daemon_approves_a_level_for_an_agent_on_the_record(self, tmp_path: Path) -> None:
        """The whole of it with the real plan service and no API in sight:
        the approve lands, the record names the agent, and a second approve
        against the revision the first one read is refused on the record."""
        h = Harness(tmp_path)
        h.loop.recover()
        plan_id, root, (a, b) = self._plan_with_children(h, "A", "B")
        revision = h.loop.plans.get(plan_id).revision

        def approve() -> tuple[str, Any]:
            return record_plan_operation(
                h.loop.operations,
                plan_spec(
                    target_key=plan_id,
                    request={
                        "plan_id": plan_id,
                        "node_id": root,
                        "expected_revision": revision,
                        "node_ids": None,
                    },
                    expected_revision=revision,
                ),
                call=lambda: h.loop.plans.approve(
                    plan_id,
                    root,
                    expected_revision=revision,
                    node_ids=None,
                    now=h.clock(),
                    actor=CRITIC.audit(),
                ),
                result=lambda plan: {"revision": plan.revision},
                clock=h.clock,
                generation=h.loop.generation,
            )

        op_id, plan = approve()
        assert {plan.node(n).state for n in (a, b)} == {"approved"}
        done = h.loop.operations.get(op_id)
        assert done is not None and done.state == "succeeded"
        assert done.actor["id"] == "agent:critic" and done.result == {"revision": plan.revision}
        assert done.claimed_generation == h.loop.generation
        with pytest.raises(PlanRefusal) as excinfo:
            approve()
        refused = h.loop.operations.get(excinfo.value.extra["operation_id"])
        assert refused is not None and refused.id != op_id
        assert (refused.state, refused.error_code) == ("failed", "stale_revision")
        assert refused.result == {"status": 409, "extra": {"current_revision": plan.revision}}

    def test_a_claimed_approve_of_a_plan_that_is_gone_is_failed(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        op_id = self._claimed_approve(h, "plan_gone", "node_gone", None)
        h.loop.recover()
        settled = h.loop.operations.get(op_id)
        assert settled is not None and (settled.state, settled.error_code) == (
            "failed",
            "unknown_target",
        )

    def test_a_claimed_grant_create_is_judged_from_the_stored_grant(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        grant = _grant(h)
        written, _ = h.loop.operations.accept(
            spec(action="grant.create", target_kind="grant", target_key=grant.id), now=1.0
        )
        lost, _ = h.loop.operations.accept(
            spec(action="grant.create", target_kind="grant", target_key="grant_lost"), now=1.5
        )
        for op in (written, lost):
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert h.loop.operations.get(written.id).state == "succeeded"  # type: ignore[union-attr]
        judged = h.loop.operations.get(lost.id)
        assert judged is not None and judged.state == "failed"
        assert judged.error_code == "interrupted_before_effect"
        assert judged.error_detail == "the grant was not written"

    @pytest.mark.parametrize(
        ("applied", "changes", "expected", "code"),
        [
            # The edit landed: the grant holds it and its revision moved.
            (
                {"daily_limit": 9, "enabled": False},
                {"daily_limit": 9, "enabled": False},
                "succeeded",
                None,
            ),
            ({"note": None}, {"note": None}, "succeeded", None),
            (
                {"conditions": Conditions(levels=("task",))},
                {"conditions": {"levels": ["task"]}},
                "succeeded",
                None,
            ),
            # It never landed: the grant is where the caller read it.
            (None, {"daily_limit": 9}, "failed", "interrupted_before_effect"),
            # Someone else's edit landed instead: the grant moved, without this change.
            ({"note": "theirs"}, {"daily_limit": 9}, "failed", "interrupted_before_effect"),
        ],
    )
    def test_a_claimed_grant_update_is_judged_from_what_the_grant_holds(
        self,
        tmp_path: Path,
        applied: dict[str, Any] | None,
        changes: dict[str, Any],
        expected: str,
        code: str | None,
    ) -> None:
        h = Harness(tmp_path)
        grant = _grant(h)
        if applied is not None:
            h.loop.delegation.update_grant(grant.id, applied, expected_revision=1, now=2.0)
        op, _ = h.loop.operations.accept(
            spec(
                action="grant.update",
                target_kind="grant",
                target_key=grant.id,
                request={"changes": changes},
                expected_revision=1,
            ),
            now=1.0,
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and (settled.state, settled.error_code) == (expected, code)
        if expected == "failed":
            assert "does not hold the requested change" in str(settled.error_detail)

    def test_a_claimed_edit_of_a_grant_that_is_gone_fails_by_name(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(
            spec(
                action="grant.update",
                target_kind="grant",
                target_key="grant_gone",
                request={"changes": {"enabled": False}},
                expected_revision=1,
            ),
            now=1.0,
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None
        assert (settled.state, settled.error_code) == ("failed", "unknown_target")

    @pytest.mark.parametrize(("removed", "expected"), [(True, "succeeded"), (False, "failed")])
    def test_a_claimed_grant_delete_is_judged_from_the_grant_being_gone(
        self, tmp_path: Path, removed: bool, expected: str
    ) -> None:
        h = Harness(tmp_path)
        grant = _grant(h)
        if removed:
            h.loop.delegation.delete_grant(grant.id)
        op, _ = h.loop.operations.accept(
            spec(action="grant.delete", target_kind="grant", target_key=grant.id), now=1.0
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == expected
        if not removed:
            assert settled.error_code == "interrupted_before_effect"
            assert settled.error_detail == "the grant is still there"

    def test_no_grant_write_is_left_reconciling(self, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        for action in ("grant.create", "grant.update", "grant.delete"):
            op, _ = h.loop.operations.accept(
                spec(action=action, target_kind="grant", target_key="grant_x"), now=1.0
            )
            assert op.effect
            h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        assert "reconciling" not in {o.state for o in h.loop.operations.recent()}

    def test_what_evidence_cannot_decide_is_reconciling_never_succeeded(
        self, tmp_path: Path
    ) -> None:
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(
            spec(action="run.review_resume", target_kind="target", target_key="r1"), now=1.0
        )
        h.loop.operations.claim(op.id, "g_dead", now=2.0)
        h.loop.recover()
        settled = h.loop.operations.get(op.id)
        assert settled is not None and settled.state == "reconciling"
        assert settled.error_detail == "the effect could not be established from the record"
        # And it stays that way across the next recovery: no second guess.
        h.loop.recover()
        assert h.loop.operations.get(op.id).state == "reconciling"  # type: ignore[union-attr]

    def test_the_record_survives_the_stamp_being_rewound(self, tmp_path: Path) -> None:
        """A rollback reinstalls the previous release against this
        database and re-runs the revision on the way back: the tables it
        meets are kept, rows and all."""
        h = Harness(tmp_path)
        op, _ = h.loop.operations.accept(spec(), now=1.0)
        h.dstore.close()
        h.store.close()
        with sqlite3.connect(h.config.paths.state_db) as conn:
            conn.execute("UPDATE alembic_version SET version_num = '0008'")
        reopened = DaemonStore(h.config.paths.state_db)
        rows = OperationStore(reopened).recent()
        assert [o.id for o in rows] == [op.id]
        assert json.loads(json.dumps(rows[0].request)) == {"hold": "operator"}
