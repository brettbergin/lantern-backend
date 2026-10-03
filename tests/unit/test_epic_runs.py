"""Epic runs on the daemon loop (#2347): a published epic's tasks admitted as
issue runs in dependency order, with no queueing label, each landed code task
or delivered workload task making its dependents ready, and a failed task's
dependents never admitted.

The loop is the tests' ``Harness`` (real stores, a scripted runner); the
forge is the issue source over a recording ``GithubOps`` stand-in, so the
labels and comments a claim and a report write are the ones checked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from lantern.config import Config
from lantern.daemon.model import WorkItem, is_epic_run_id
from lantern.daemon.sources import GitHubIssueSource
from lantern.db.api_models import ApiEventRow
from lantern.plans.epicrun import EpicRun, from_item, readiness
from lantern.plans.model import ForgeRef, Plan, PlanNode
from lantern.plans.service import PlanRefusal
from lantern.plans.store import PlanStore
from tests.unit.test_daemon_loop import Harness
from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue, report

ACTOR = {"kind": "client", "id": "c1", "display": "Ada"}
EPIC_NUMBER = 10


def _node(node_id: str, number: int, **fields: Any) -> PlanNode:
    return PlanNode(
        id=node_id,
        plan_id="plan_1",
        parent_id="epic",
        position=number,
        level="task",
        repository="o/r",
        state="published",
        origin="person",
        title=node_id.upper(),
        forge=ForgeRef(number=number, url=f"https://x/issues/{number}", state="open"),
        **{"kind": "code", **fields},
    )


def _plan(h: Harness, *tasks: PlanNode) -> Plan:
    epic = PlanNode(
        id="epic",
        plan_id="plan_1",
        parent_id=None,
        position=0,
        level="epic",
        repository="o/r",
        state="published",
        origin="person",
        title="The epic",
        forge=ForgeRef(number=EPIC_NUMBER, url="https://x/issues/10", state="open"),
    )
    plan = Plan(
        id="plan_1",
        workspace_id="default",
        root_id="epic",
        archived=False,
        created_by="c1",
        created_by_display="Ada",
        created_at=h.clock(),
        updated_at=h.clock(),
        revision=3,
        nodes=(epic, *tasks),
    )
    return PlanStore(h.dstore).create(plan, events=[], actor=None)


def _harness(tmp_path: Path, ops: RecordingOps, **config: Any) -> Harness:
    cfg = Config.model_validate(
        {
            "home": str(tmp_path / "state"),
            "github": {"repo": "o/r"},
            "daemon": {
                "trigger_label": "lantern:run",
                "in_progress_label": "lantern:in-progress",
                **config.pop("daemon", {}),
            },
            **config,
        }
    )
    h = Harness(tmp_path, cfg)
    h.loop.source = GitHubIssueSource(
        lambda: ops,  # type: ignore[arg-type]
        "o/r",
        LABELS,
        host="db",
        clock=lambda: FIXTURE_NOW,
    )
    return h


def _issues(*numbers: int) -> RecordingOps:
    return RecordingOps({str(n): issue(n, "sbx:task") for n in numbers})


def _start(h: Harness) -> EpicRun:
    return h.loop.epic_runs.start("plan_1", "epic", expected_revision=3, actor=ACTOR, now=h.clock())


def _states(h: Harness, run: EpicRun) -> dict[str, str]:
    now = h.loop.epic_runs.runs.get(run.id)
    assert now is not None
    return {t.node_id: t.state for t in now.tasks}


def _events(h: Harness, prefix: str = "plan.run.") -> list[tuple[str, dict[str, Any]]]:
    with h.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type.like(f"{prefix}%")).order_by(ApiEventRow.seq)
        )
        return [(str(r.type), json.loads(r.data_json)) for r in rows]


def _labels_added(ops: RecordingOps) -> set[str]:
    return {
        label
        for method, path, body in ops.raw_calls
        if method == "POST" and path.endswith("/labels")
        for label in (body or {}).get("labels") or []
    }


def _drain(h: Harness, ticks: int = 10) -> None:
    for _ in range(ticks):
        h.loop.tick()


class TestAdmission:
    def test_ready_tasks_are_queued_together_and_dependents_wait(self, tmp_path: Path) -> None:
        ops = _issues(11, 12, 13)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)), _node("c", 13))
        run = _start(h)
        assert is_epic_run_id(run.id) and run.state == "running"
        assert _states(h, run) == {"a": "queued", "b": "waiting", "c": "queued"}
        items = {i.source_key: i for i in h.dstore.items()}
        assert set(items) == {"11", "13"}
        for item in items.values():
            assert item.kind == "code" and item.state == "queued"
            assert item.parent_item_id == run.id and item.from_epic_run
            assert item.origin_agent is None and item.chain_depth == 0
        # Nothing touched a label: the issues are inert to every poll.
        assert _labels_added(ops) == set()
        assert h.loop.source.poll() == []
        started, *admitted = _events(h)
        assert started == (
            "plan.run.started",
            {"plan_id": "plan_1", "node_id": "epic", "epic_run_id": run.id},
        )
        assert [(t, d["task_node_id"], d["item_id"]) for t, d in admitted] == [
            ("plan.run.task_admitted", "a", items["11"].item_id),
            ("plan.run.task_admitted", "c", items["13"].item_id),
        ]

    def test_a_task_already_queued_is_adopted_not_admitted_twice(self, tmp_path: Path) -> None:
        ops = _issues(11)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11))
        # A person labelled the issue before the run started.
        ops.issues["11"]["labels"].append({"name": "lantern:run"})
        h.dstore.upsert_new(h.loop.source.poll()[0], h.clock())
        run = _start(h)
        (item,) = h.dstore.items()
        assert _states(h, run) == {"a": "queued"}
        assert item.parent_item_id == run.id

    def test_refusals_before_anything_is_admitted(self, tmp_path: Path) -> None:
        ops = _issues(11)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11))
        with pytest.raises(PlanRefusal) as stale:
            h.loop.epic_runs.start("plan_1", "epic", expected_revision=2, actor=ACTOR, now=1.0)
        assert stale.value.code == "stale_revision"
        with pytest.raises(PlanRefusal) as task:
            h.loop.epic_runs.start("plan_1", "a", expected_revision=3, actor=ACTOR, now=1.0)
        assert task.value.status == 422
        run = _start(h)
        with pytest.raises(PlanRefusal) as again:
            _start(h)
        assert again.value.code == "already_running"
        assert again.value.extra["epic_run_id"] == run.id
        assert len(h.dstore.items()) == 1


class TestCompletion:
    def test_a_landed_code_task_makes_its_dependents_ready(self, tmp_path: Path) -> None:
        ops = _issues(11, 12, 13)
        h = _harness(tmp_path, ops)
        _plan(
            h,
            _node("a", 11),
            _node("b", 12, depends_on=("a",)),
            _node("c", 13, depends_on=("b",)),
        )
        run = _start(h)
        h.loop.tick()  # A is claimed, runs and merges; B still waits
        assert [(i.source_key, i.state) for i in h.dstore.items()] == [("11", "done")]
        assert _states(h, run) == {"a": "queued", "b": "waiting", "c": "waiting"}
        h.loop.tick()  # the next pass sees A landed and queues B, which runs
        assert _states(h, run) == {"a": "landed", "b": "queued", "c": "waiting"}
        _drain(h)
        assert _states(h, run) == {"a": "landed", "b": "landed", "c": "landed"}
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed" and final.completed_at
        assert all(t.run_id for t in final.tasks)
        # Admitted one at a time, each after what it depends on landed.
        admitted = [d["task_node_id"] for t, d in _events(h) if t == "plan.run.task_admitted"]
        assert admitted == ["a", "b", "c"]
        assert _events(h)[-1][0] == "plan.run.completed"
        # Each issue was claimed (in progress), closed by the merge report,
        # and never wore the trigger label.
        added = _labels_added(ops)
        assert "lantern:in-progress" in added and "lantern:completed" in added
        assert "lantern:run" not in added and "lantern:workload" not in added
        closed = {p for m, p, b in ops.raw_calls if m == "PATCH" and (b or {}).get("state")}
        assert closed == {f"/repos/o/r/issues/{n}" for n in (11, 12, 13)}
        assert all("claimed" in body for _, body in ops.comments if "lantern-claim" in body)

    def test_a_delivered_workload_task_makes_its_dependents_ready(self, tmp_path: Path) -> None:
        ops = _issues(11, 12)
        h = _harness(tmp_path, ops, workloads=[{"name": "research", "sinks": ["chat", "issue"]}])
        _plan(
            h,
            _node("a", 11, kind="workload", workload_profile="research"),
            _node("b", 12, depends_on=("a",)),
        )
        h.outcomes = ["completed", "merged"]
        ran: list[WorkItem] = []
        inner = h.loop._runner

        def runner(item: WorkItem, *args: Any) -> Any:
            ran.append(item)
            return inner(item, *args)

        h.loop._runner = runner
        run = _start(h)
        (item,) = h.dstore.items()
        assert item.kind == "workload" and item.profile == "research"
        _drain(h, 4)
        assert _states(h, run) == {"a": "landed", "b": "landed"}
        # The workload ran under its profile, the same as the label path;
        # its dependent ran as a code run.
        assert [(i.source_key, i.kind, i.profile) for i in ran] == [
            ("11", "workload", "research"),
            ("12", "code", None),
        ]
        # The workload's completed report closed its issue.
        assert ("PATCH", "/repos/o/r/issues/11") in {(m, p) for m, p, _ in ops.raw_calls}
        assert "lantern:workload" not in _labels_added(ops)

    def test_a_failed_task_does_not_admit_its_dependents(self, tmp_path: Path) -> None:
        ops = _issues(11, 12, 13, 14)
        h = _harness(tmp_path, ops, daemon={"max_attempts_per_item": 1})
        _plan(
            h,
            _node("a", 11),
            _node("b", 12, depends_on=("a",)),
            _node("c", 13, depends_on=("b",)),
            _node("d", 14),
        )
        h.outcomes = ["failed", "merged"]
        run = _start(h)
        _drain(h)
        states = _states(h, run)
        assert states == {"a": "failed", "b": "blocked", "c": "blocked", "d": "landed"}
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "running"
        assert final.task("a") is not None and final.task("a").reason  # type: ignore[union-attr]
        assert {i.source_key for i in h.dstore.items()} == {"11", "14"}
        admitted = [d["task_node_id"] for t, d in _events(h) if t == "plan.run.task_admitted"]
        assert admitted == ["a", "d"]

    def test_a_task_whose_issue_is_closed_counts_as_done(self, tmp_path: Path) -> None:
        ops = _issues(11, 12)
        ops.issues["11"]["state"] = "closed"
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)))
        run = _start(h)
        assert _states(h, run) == {"a": "closed", "b": "queued"}
        _drain(h, 3)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"
        (_, completed) = _events(h)[-1]
        assert completed["landed"] == ["b"] and completed["closed"] == ["a"]


class TestRules:
    def test_readiness_waits_blocks_and_releases(self) -> None:
        node = _node("b", 12, depends_on=("a", "x"))
        assert readiness(node, {"a": "running"}) == "waiting"
        assert readiness(node, {"a": "failed"}) == "blocked"
        assert readiness(node, {"a": "blocked"}) == "blocked"
        assert readiness(node, {"a": "landed"}) == "ready"
        assert readiness(node, {"a": "closed"}) == "ready"

    def test_an_item_reads_as_the_task_it_was_admitted_for(self) -> None:
        def item(state: str, error: str | None = None) -> WorkItem:
            return WorkItem(
                item_id="gh:issue:1",
                source_key="1",
                title="x",
                state=state,  # type: ignore[arg-type]
                last_error=error,
            )

        assert from_item(None) is None
        assert from_item(item("queued")) == ("queued", None)
        assert from_item(item("gated")) == ("running", None)
        assert from_item(item("done")) == ("landed", None)
        assert from_item(item("blocked", "refused")) == ("failed", "refused")

    def test_only_an_epic_runs_item_is_claimed_without_the_trigger_label(
        self, tmp_path: Path
    ) -> None:
        ops = _issues(11)
        h = _harness(tmp_path, ops)
        source = h.loop.source
        plain = source.admit("o/r", "11", "code", label=False)
        assert _labels_added(ops) == set()
        assert source.claim(plain) is False
        mine = plain.model_copy(update={"parent_item_id": "erun_0123456789abcdef"})
        assert source.claim(mine) is True
        assert _labels_added(ops) == {"lantern:in-progress"}
        assert not any(m == "DELETE" and "/labels/" in p for m, p, _ in ops.raw_calls)


# -- pausing, retrying, skipping and stopping (#2348) ---------------------------------


def _types(h: Harness, since: int = 0) -> list[str]:
    return [t for t, _ in _events(h)[since:]]


def _failed_chain(tmp_path: Path) -> tuple[Harness, RecordingOps, EpicRun]:
    """A fails (one attempt); B depends on A, C on B; D is a sibling and E
    depends on D. After the drain A is failed and D and E have landed."""
    ops = _issues(11, 12, 13, 14, 15)
    h = _harness(tmp_path, ops, daemon={"max_attempts_per_item": 1})
    _plan(
        h,
        _node("a", 11),
        _node("b", 12, depends_on=("a",)),
        _node("c", 13, depends_on=("b",)),
        _node("d", 14),
        _node("e", 15, depends_on=("d",)),
    )
    h.outcomes = ["failed"]
    run = _start(h)
    _drain(h)
    return h, ops, run


class TestAFailure:
    def test_only_the_failed_tasks_dependents_are_blocked(self, tmp_path: Path) -> None:
        h, _, run = _failed_chain(tmp_path)
        assert _states(h, run) == {
            "a": "failed",
            "b": "blocked",
            "c": "blocked",
            "d": "landed",
            "e": "landed",
        }
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "running"
        # Each blocked task names the dependency it waits on a person for.
        assert final.task("b").reason == "blocked by A (failed)"  # type: ignore[union-attr]
        assert final.task("c").reason == "blocked by B (blocked)"  # type: ignore[union-attr]
        events = _events(h)
        (failed,) = [d for t, d in events if t == "plan.run.task_failed"]
        assert failed["task_node_id"] == "a" and failed["from"] == "queued"
        assert failed["reason"]
        blocked = {d["task_node_id"]: d for t, d in events if t == "plan.run.task_blocked"}
        assert blocked["b"]["blocked_by"] == ["a"] and blocked["c"]["blocked_by"] == ["b"]
        # The run is not paused — D and E went on — but the person who
        # started it hears that it needs them.
        (paused,) = [d for t, d in events if t == "plan.run.paused"]
        assert paused["reason"] == "task_failed" and paused["state"] == "running"
        assert paused["task_node_id"] == "a" and paused["blocked"] == ["b", "c"]
        assert paused["error"] == failed["reason"]
        landed = [d["task_node_id"] for t, d in events if t == "plan.run.task_landed"]
        assert landed == ["d", "e"]
        running = [d["task_node_id"] for t, d in events if t == "plan.run.task_running"]
        assert running == []  # the harness runs a dispatch to its end within one tick

    def test_every_task_move_records_one_event(self, tmp_path: Path) -> None:
        h, _, run = _failed_chain(tmp_path)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None
        moves: dict[str, list[str]] = {}
        for kind, data in _events(h):
            if kind.startswith("plan.run.task_"):
                moves.setdefault(data["task_node_id"], []).append(data["state"])
        # The states each task passed through, as its events tell them,
        # end where the run says each task is.
        assert {k: v[-1] for k, v in moves.items()} == _states(h, run)
        # A run's tasks start out waiting; E is admitted once D landed.
        assert moves["e"] == ["queued", "landed"]


class TestRetry:
    def test_retry_requeues_the_item_and_its_dependents_wait_on_it(self, tmp_path: Path) -> None:
        h, ops, run = _failed_chain(tmp_path)
        (item_a,) = [i for i in h.dstore.items() if i.source_key == "11"]
        assert item_a.state == "failed" and item_a.claimed
        since = len(_events(h))
        retried = h.loop.epic_runs.retry("plan_1", "a", actor=ACTOR, now=h.clock())
        assert {t.node_id: t.state for t in retried.tasks} == {
            "a": "queued",
            "b": "waiting",
            "c": "waiting",
            "d": "landed",
            "e": "landed",
        }
        fresh = h.dstore.get(item_a.item_id)
        assert fresh is not None and fresh.state == "queued" and fresh.attempts == 0
        assert fresh.parent_item_id == run.id
        events = _events(h)[since:]
        assert [t for t, _ in events] == [
            "plan.run.task_retried",
            "plan.run.task_waiting",
            "plan.run.task_waiting",
        ]
        assert events[0][1]["via"] == "item" and events[0][1]["by"] == "Ada"
        # The item retry told the issue who asked and cleared the failed label.
        assert any(f"Re-queued by Ada (epic run {run.id})" in b for _, b in ops.comments)
        assert ("DELETE", "/repos/o/r/issues/11/labels/lantern%3Afailed") in {
            (m, p) for m, p, _ in ops.raw_calls
        }
        _drain(h)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"
        assert _types(h)[-1] == "plan.run.completed"

    def test_a_refused_admission_is_admitted_afresh(self, tmp_path: Path) -> None:
        ops = _issues(11, 12)
        ops.issues["11"]["pull_request"] = {"url": "x"}
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)))
        run = _start(h)
        assert _states(h, run) == {"a": "failed", "b": "blocked"}
        assert h.dstore.items() == []
        del ops.issues["11"]["pull_request"]
        since = len(_events(h))
        retried = h.loop.epic_runs.retry("plan_1", "a", actor=ACTOR, now=h.clock())
        assert {t.node_id: t.state for t in retried.tasks} == {"a": "queued", "b": "waiting"}
        assert _types(h, since) == [
            "plan.run.task_retried",
            "plan.run.task_admitted",
            "plan.run.task_waiting",
        ]
        assert _events(h)[since][1]["via"] == "admission"
        (item,) = h.dstore.items()
        assert item.parent_item_id == run.id

    def test_only_a_failed_task_is_retried(self, tmp_path: Path) -> None:
        h, _, _ = _failed_chain(tmp_path)
        driver = h.loop.epic_runs
        with pytest.raises(PlanRefusal) as blocked:
            driver.retry("plan_1", "c", actor=ACTOR, now=h.clock())
        assert blocked.value.code == "task_blocked"
        assert blocked.value.extra["blocked_by"] == ["b"]
        with pytest.raises(PlanRefusal) as landed:
            driver.retry("plan_1", "d", actor=ACTOR, now=h.clock())
        assert landed.value.code == "task_not_failed"
        with pytest.raises(PlanRefusal) as epic:
            driver.retry("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert epic.value.status == 422


class TestSkip:
    def test_skip_treats_the_task_as_done_and_leaves_its_issue(self, tmp_path: Path) -> None:
        h, ops, run = _failed_chain(tmp_path)
        writes = [c for c in ops.raw_calls if c[0] != "GET" and "/issues/11" in c[1]]
        since = len(_events(h))
        skipped = h.loop.epic_runs.skip("plan_1", "a", actor=ACTOR, now=h.clock())
        states = {t.node_id: t.state for t in skipped.tasks}
        assert states["a"] == "skipped" and states["b"] == "queued"
        assert skipped.task("a").reason == "skipped by Ada"  # type: ignore[union-attr]
        types = _types(h, since)
        assert types[0] == "plan.run.task_skipped"
        assert types[1:] == ["plan.run.task_admitted", "plan.run.task_waiting"]
        _drain(h)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"
        (_, completed) = _events(h)[-1]
        assert completed["skipped"] == ["a"]
        # The skipped task's issue and item are as its failure left them.
        assert [c for c in ops.raw_calls if c[0] != "GET" and "/issues/11" in c[1]] == writes
        (item_a,) = [i for i in h.dstore.items() if i.source_key == "11"]
        assert item_a.state == "failed"

    def test_a_task_under_way_or_done_is_not_skipped(self, tmp_path: Path) -> None:
        ops = _issues(11, 12)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)))
        _start(h)
        with pytest.raises(PlanRefusal) as queued:
            h.loop.epic_runs.skip("plan_1", "a", actor=ACTOR, now=h.clock())
        assert queued.value.code == "task_in_progress"
        # A waiting task can be skipped: B is done without running.
        skipped = h.loop.epic_runs.skip("plan_1", "b", actor=ACTOR, now=h.clock())
        assert skipped.task("b").state == "skipped"  # type: ignore[union-attr]
        with pytest.raises(PlanRefusal) as again:
            h.loop.epic_runs.skip("plan_1", "b", actor=ACTOR, now=h.clock())
        assert again.value.code == "task_settled"


class TestPause:
    def test_pause_stops_admission_and_resume_restarts_it(self, tmp_path: Path) -> None:
        ops = _issues(11, 12)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)))
        run = _start(h)
        since = len(_events(h))
        paused = h.loop.epic_runs.pause("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert paused.state == "paused"
        assert _events(h)[since] == (
            "plan.run.paused",
            {
                "plan_id": "plan_1",
                "node_id": "epic",
                "epic_run_id": run.id,
                "reason": "person",
                "state": "paused",
                "by": "Ada",
            },
        )
        with pytest.raises(PlanRefusal) as twice:
            h.loop.epic_runs.pause("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert twice.value.code == "already_paused"
        # What was queued goes on and is followed; what it made ready is
        # not admitted while the run is paused.
        _drain(h, 4)
        assert _states(h, run) == {"a": "landed", "b": "ready"}
        assert [i.source_key for i in h.dstore.items()] == ["11"]
        since = len(_events(h))
        resumed = h.loop.epic_runs.resume("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert resumed.state == "running"
        assert resumed.task("b").state == "queued"  # type: ignore[union-attr]
        assert _types(h, since) == ["plan.run.resumed", "plan.run.task_admitted"]
        with pytest.raises(PlanRefusal) as not_paused:
            h.loop.epic_runs.resume("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert not_paused.value.code == "not_paused"
        _drain(h, 3)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"

    def test_only_an_epic_is_paused(self, tmp_path: Path) -> None:
        h = _harness(tmp_path, _issues(11))
        _plan(h, _node("a", 11))
        _start(h)
        with pytest.raises(PlanRefusal) as task:
            h.loop.epic_runs.pause("plan_1", "a", actor=ACTOR, now=h.clock())
        assert task.value.status == 422


class TestCancel:
    def test_cancel_withdraws_queued_items_and_admits_nothing_more(self, tmp_path: Path) -> None:
        ops = _issues(11, 12, 13)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12), _node("c", 13, depends_on=("a",)))
        _start(h)
        before = list(ops.raw_calls)
        since = len(_events(h))
        cancelled = h.loop.epic_runs.cancel("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert cancelled.state == "cancelled" and cancelled.completed_at
        assert {t.node_id: t.state for t in cancelled.tasks} == {
            "a": "cancelled",
            "b": "cancelled",
            "c": "cancelled",
        }
        assert cancelled.task("a").reason.startswith("withdrawn: ")  # type: ignore[union-attr]
        assert cancelled.task("c").reason.startswith("never admitted: ")  # type: ignore[union-attr]
        assert {i.source_key: i.state for i in h.dstore.items()} == {"11": "failed", "12": "failed"}
        # The person who stopped the run has seen what they stopped: the
        # withdrawn items rest in `failed` without asking for attention.
        marks = h.dstore.work_marks(item_ids=[i.item_id for i in h.dstore.items()])
        assert len(marks) == 2
        assert {(m.mark, m.cause) for m in marks.values()} == {("dismissed", "abandoned")}
        assert all(m.actor == dict(ACTOR) for m in marks.values())
        # Nothing had been written to the issues, and nothing is now.
        assert [c for c in ops.raw_calls[len(before) :] if c[0] != "GET"] == []
        events = _events(h)[since:]
        assert [t for t, _ in events] == ["plan.run.task_cancelled"] * 3 + ["plan.run.cancelled"]
        assert events[-1][1]["withdrawn"] == ["a", "b"] and events[-1][1]["running"] == []
        _drain(h, 3)
        assert h.runs == [] and len(h.dstore.items()) == 2
        with pytest.raises(PlanRefusal) as ended:
            h.loop.epic_runs.retry("plan_1", "a", actor=ACTOR, now=h.clock())
        assert ended.value.code == "run_ended"
        with pytest.raises(PlanRefusal) as again:
            h.loop.epic_runs.cancel("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert again.value.code == "run_ended"

    def test_a_task_under_way_is_left_to_finish_and_followed(self, tmp_path: Path) -> None:
        ops = _issues(11, 12)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)))
        run = _start(h)
        (item,) = h.dstore.items()
        h.dstore.mark_running(item.item_id, "run-a", h.clock())
        cancelled = h.loop.epic_runs.cancel("plan_1", "epic", actor=ACTOR, now=h.clock())
        assert {t.node_id: t.state for t in cancelled.tasks} == {"a": "running", "b": "cancelled"}
        (_, stopped) = _events(h)[-1]
        assert stopped["withdrawn"] == [] and stopped["running"] == ["a"]
        still = h.dstore.get(item.item_id)
        assert still is not None and still.state == "running"
        # The run is still followed until that task settles; its failure is
        # recorded, but a stopped run asks nobody for anything.
        assert [r.id for r in h.loop.epic_runs.runs.active()] == [run.id]
        h.dstore.abandon(item.item_id, "gave up", h.clock())
        since = len(_events(h))
        h.loop.epic_runs.tick(h.clock())
        assert _types(h, since) == ["plan.run.task_failed"]
        assert h.loop.epic_runs.runs.active() == []


class TestIssueWording:
    def _epic_item(self, source: GitHubIssueSource, *, claimed: bool) -> WorkItem:
        item = source.admit("o/r", "11", "code", label=False)
        return item.model_copy(
            update={"parent_item_id": "erun_0123456789abcdef", "claimed": claimed}
        )

    def test_an_epic_runs_task_is_sent_back_to_its_plan(self, tmp_path: Path) -> None:
        ops = _issues(11)
        h = _harness(tmp_path, ops)
        source = h.loop.source
        item = self._epic_item(source, claimed=False)
        assert source.claim(item) is True
        (claim,) = [b for _, b in ops.comments if "lantern-claim" in b]
        assert "Started as a task of epic run `erun_0123456789abcdef`" in claim
        claimed = item.model_copy(update={"claimed": True})
        source.report_abandoned(claimed, "tests failed")
        source.report_blocked(claimed, "branch protection refused", None, "")
        source.report_cancelled(claimed, report(state="cancelled", pr=None))
        _, abandoned, blocked, cancelled = [b for _, b in ops.comments]
        for body in (abandoned, blocked, cancelled):
            assert "epic run `erun_0123456789abcdef`" in body
            assert "retry it from its plan" in body
            assert "Re-add" not in body and "re-add" not in body

    def test_a_withdrawn_unclaimed_task_writes_nothing(self, tmp_path: Path) -> None:
        ops = _issues(11)
        h = _harness(tmp_path, ops)
        h.loop.source.report_abandoned(self._epic_item(h.loop.source, claimed=False), "stopped")
        assert [c for c in ops.raw_calls if c[0] != "GET"] == []


class ClosingOps(RecordingOps):
    """The recording stand-in, with an issue close that sticks the way
    GitHub's does: what completion reads back after a merge report."""

    def raw(self, method: str, path: str, body: Any = None) -> Any:
        answer = super().raw(method, path, body)
        if method == "PATCH" and (body or {}).get("state") == "closed":
            number = path.rsplit("/", 1)[-1]
            if number in self.issues:
                self.issues[number]["state"] = "closed"
        return answer


def _closing(*numbers: int) -> ClosingOps:
    ops = ClosingOps({str(n): issue(n, "sbx:task") for n in numbers})
    ops.issues[str(EPIC_NUMBER)] = issue(EPIC_NUMBER, "sbx:epic")
    return ops


def _summaries(ops: RecordingOps) -> list[str]:
    return [b for n, b in ops.comments if n == EPIC_NUMBER and "sbx-plan-summary" in b]


def _epic_closes(ops: RecordingOps) -> list[Any]:
    return [
        b
        for m, p, b in ops.raw_calls
        if m == "PATCH" and p == f"/repos/o/r/issues/{EPIC_NUMBER}" and (b or {}).get("state")
    ]


class TestClosingTheEpic:
    def test_a_completed_run_summarises_and_closes_its_epic(self, tmp_path: Path) -> None:
        ops = _closing(11, 12)
        h = _harness(tmp_path, ops)
        h.loop.epic_runs.forge = lambda: ops
        _plan(h, _node("a", 11), _node("b", 12, depends_on=("a",)))
        run = _start(h)
        _drain(h)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"
        (summary,) = _summaries(ops)
        assert "<!-- sbx-plan-summary: plan_1/epic -->" in summary
        assert "- #11 A — landed" in summary and "- #12 B — landed" in summary
        assert f"Epic run `{run.id}`, started by Ada." in summary
        assert _epic_closes(ops) == [{"state": "closed", "state_reason": "completed"}]
        plan = PlanStore(h.dstore).get("plan_1")
        assert plan is not None
        assert {n.id: n.forge.state for n in plan.nodes if n.forge} == {
            "epic": "closed",
            "a": "closed",
            "b": "closed",
        }
        changes = [(d["node_id"], d["change"]) for _, d in _events(h, "plan.node.changed")]
        assert changes == [("a", "closed"), ("b", "closed"), ("epic", "completed")]
        # Later ticks and sweeps write nothing more.
        h.clock.t += 3600
        _drain(h, 3)
        assert len(_summaries(ops)) == 1 and len(_epic_closes(ops)) == 1

    def test_a_skipped_task_left_open_holds_the_epic_until_a_person_closes_it(
        self, tmp_path: Path
    ) -> None:
        ops = _closing(11, 12)
        h = _harness(tmp_path, ops, daemon={"max_attempts_per_item": 1})
        h.loop.epic_runs.forge = lambda: ops
        _plan(h, _node("a", 11), _node("b", 12))
        h.outcomes = ["failed", "merged"]
        run = _start(h)
        _drain(h)
        h.loop.epic_runs.skip("plan_1", "a", actor=ACTOR, now=h.clock())
        _drain(h, 3)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"
        # The run is done, but A's issue is still open on the forge.
        assert ops.issues["11"]["state"] == "open"
        assert _summaries(ops) == [] and _epic_closes(ops) == []

        ops.issues["11"]["state"] = "closed"  # a person closes it
        h.clock.t += 601  # the sweep looks again
        h.loop.tick()
        (summary,) = _summaries(ops)
        assert "- #11 A — skipped in the epic run, then closed on the forge" in summary
        assert len(_epic_closes(ops)) == 1

    def test_close_completed_off_leaves_the_epic_open(self, tmp_path: Path) -> None:
        ops = _closing(11)
        h = _harness(tmp_path, ops, planning={"close_completed": False})
        h.loop.epic_runs.forge = lambda: ops
        _plan(h, _node("a", 11))
        run = _start(h)
        _drain(h)
        final = h.loop.epic_runs.runs.get(run.id)
        assert final is not None and final.state == "completed"
        assert _summaries(ops) == [] and _epic_closes(ops) == []

    def test_a_task_that_lands_outside_an_epic_run_can_finish_its_epic(
        self, tmp_path: Path
    ) -> None:
        ops = _closing(11)
        h = _harness(tmp_path, ops)
        h.loop.epic_runs.forge = lambda: ops
        _plan(h, _node("a", 11))
        # A person started the task on its own, with the trigger label.
        ops.issues["11"]["labels"].append({"name": "lantern:run"})
        _drain(h, 3)
        assert [(i.source_key, i.state) for i in h.dstore.items()] == [("11", "done")]
        assert h.loop.epic_runs.runs.latest("plan_1", "epic") is None
        (summary,) = _summaries(ops)
        assert "No epic run took part" in summary and "- #11 A — closed" in summary
        assert len(_epic_closes(ops)) == 1


class TestATaskThatJoinsMidRun:
    def test_a_task_published_under_a_running_epic_is_admitted(self, tmp_path: Path) -> None:
        """An approved re-plan adds a task to a running epic (or an issue
        is attached to it): the run picks it up on its next pass, waits
        on its dependencies like any other, and does not complete without
        it."""
        ops = _issues(1, 2)
        h = _harness(tmp_path, ops)
        _plan(h, _node("a", 1))
        run = _start(h)
        assert _states(h, run) == {"a": "queued"}

        store = PlanStore(h.dstore)
        plan = store.get("plan_1")
        assert plan is not None
        store.apply(
            plan.id,
            expected_revision=plan.revision,
            now=h.clock(),
            upsert=[_node("b", 2, depends_on=("a",))],
        )

        h.loop.epic_runs.tick(h.clock())
        assert _states(h, run) == {"a": "queued", "b": "waiting"}
        after = h.loop.epic_runs.runs.get(run.id)
        assert after is not None and after.state == "running"
        assert any(d.get("task_node_id") == "b" for _, d in _events(h)), _events(h)
