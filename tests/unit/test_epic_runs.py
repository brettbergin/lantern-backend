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

from sbxloop.config import Config
from sbxloop.daemon.model import WorkItem, is_epic_run_id
from sbxloop.daemon.sources import GitHubIssueSource
from sbxloop.db.api_models import ApiEventRow
from sbxloop.plans.epicrun import EpicRun, from_item, readiness
from sbxloop.plans.model import ForgeRef, Plan, PlanNode
from sbxloop.plans.service import PlanRefusal
from sbxloop.plans.store import PlanStore
from tests.unit.test_daemon_loop import Harness
from tests.unit.test_daemon_sources import FIXTURE_NOW, LABELS, RecordingOps, issue

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
                "trigger_label": "sbxloop:run",
                "in_progress_label": "sbxloop:in-progress",
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
        ops.issues["11"]["labels"].append({"name": "sbxloop:run"})
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
        assert "sbxloop:in-progress" in added and "sbxloop:completed" in added
        assert "sbxloop:run" not in added and "sbxloop:workload" not in added
        closed = {p for m, p, b in ops.raw_calls if m == "PATCH" and (b or {}).get("state")}
        assert closed == {f"/repos/o/r/issues/{n}" for n in (11, 12, 13)}
        assert all("claimed" in body for _, body in ops.comments if "sbxloop-claim" in body)

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
        assert "sbxloop:workload" not in _labels_added(ops)

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
        assert _labels_added(ops) == {"sbxloop:in-progress"}
        assert not any(m == "DELETE" and "/labels/" in p for m, p, _ in ops.raw_calls)
