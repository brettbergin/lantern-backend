"""Epic runs: an epic's tasks admitted as issue runs in dependency order (#2347).

A person starts an epic run on a published epic (``plans:publish``); the
daemon owns it from there (:mod:`sbxloop.daemon.epicruns` drives it each
tick). This module holds its shapes, its rows and the rules that decide
what a task is waiting for — nothing here talks to the forge or the queue.

A task is:

* ``waiting`` — a sibling it depends on is not closed yet;
* ``ready`` — every dependency is closed and it has not been admitted
  (a transient state: the driver admits it on the same pass, and a task
  stays ``ready`` only while the forge could not be reached);
* ``queued`` / ``running`` — the item it was admitted as, as the queue
  holds it (a merge gate or a review wait is still ``running``);
* ``landed`` — that item finished: a code run merged, a workload
  delivered. The source closed the issue on the way;
* ``closed`` — its issue was already closed when the run reached it;
* ``failed`` — its item gave up, was blocked or was cancelled, or the
  admission was refused by rule; the reason says which;
* ``blocked`` — a dependency failed or is blocked, so it is not admitted.

``landed`` and ``closed`` are what make a dependent ready. Everything else
is re-read from the item on every pass, so an operator's retry of a failed
item (``queued`` again) is followed, not fought.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from sqlalchemy import select, update

from sbxloop.daemon.model import EPIC_RUN_PREFIX, WorkItem
from sbxloop.daemon.store import DaemonStore
from sbxloop.db.daemon_models import PlanEpicRunRow, PlanEpicRunTaskRow, WorkItemRow
from sbxloop.plans.model import PlanNode
from sbxloop.plans.store import PlanEvent, _event, new_id

EpicRunState = Literal["running", "paused", "completed", "cancelled"]
TaskState = Literal[
    "waiting", "ready", "queued", "running", "landed", "closed", "failed", "blocked"
]

#: What makes a dependent ready.
DONE: frozenset[str] = frozenset({"landed", "closed"})
#: What keeps a dependent from ever being admitted until a person acts.
STUCK: frozenset[str] = frozenset({"failed", "blocked"})
#: A task whose item the driver still follows.
FOLLOWED: frozenset[str] = frozenset({"queued", "running", "failed"})

#: An item's state, as the task it was admitted for reads it.
_FROM_ITEM: dict[str, TaskState] = {
    "queued": "queued",
    "running": "running",
    "gated": "running",
    "awaiting_review": "running",
    "paused_review": "running",
    "done": "landed",
    "failed": "failed",
    "blocked": "failed",
    "cancelled": "failed",
}


def new_epic_run_id() -> str:
    return new_id(EPIC_RUN_PREFIX)


@dataclass(frozen=True, slots=True)
class EpicRunTask:
    node_id: str
    position: int
    state: TaskState
    item_id: str | None = None
    run_id: str | None = None
    reason: str | None = None
    admitted_at: float | None = None
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class EpicRun:
    id: str
    plan_id: str
    node_id: str
    state: EpicRunState
    started_by: str | None
    started_by_display: str | None
    created_at: float
    updated_at: float
    completed_at: float | None = None
    tasks: tuple[EpicRunTask, ...] = field(default=())

    def task(self, node_id: str) -> EpicRunTask | None:
        return next((t for t in self.tasks if t.node_id == node_id), None)


def from_item(item: WorkItem | None) -> tuple[TaskState, str | None] | None:
    """What a task admitted as ``item`` is now, and why when it failed;
    ``None`` when the row is gone (a claim that failed forgets its row):
    the task is admitted again."""
    if item is None:
        return None
    state = _FROM_ITEM.get(item.state, "running")
    return state, (item.last_error if state == "failed" else None)


def readiness(node: PlanNode, states: Mapping[str, TaskState]) -> TaskState:
    """Where a task not yet admitted stands against its dependencies:
    ``blocked`` when one failed or is blocked, ``ready`` when every one is
    closed, else ``waiting``. A dependency that is not one of the run's
    tasks cannot be followed and is not waited on (publishing refuses a
    task whose dependency is not on the forge)."""
    waiting = False
    for dep in node.depends_on:
        state = states.get(dep)
        if state is None:
            continue
        if state in STUCK:
            return "blocked"
        if state not in DONE:
            waiting = True
    return "waiting" if waiting else "ready"


def _task(row: PlanEpicRunTaskRow) -> EpicRunTask:
    return EpicRunTask(
        node_id=str(row.node_id),
        position=int(row.position),
        state=cast(TaskState, row.state),
        item_id=row.item_id,
        run_id=row.run_id,
        reason=row.reason,
        admitted_at=row.admitted_at,
        updated_at=float(row.updated_at),
    )


def _run(row: PlanEpicRunRow, tasks: Sequence[PlanEpicRunTaskRow]) -> EpicRun:
    return EpicRun(
        id=str(row.epic_run_id),
        plan_id=str(row.plan_id),
        node_id=str(row.node_id),
        state=cast(EpicRunState, row.state),
        started_by=row.started_by,
        started_by_display=row.started_by_display,
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        completed_at=row.completed_at,
        tasks=tuple(_task(t) for t in sorted(tasks, key=lambda t: (t.position, t.node_id))),
    )


def _task_columns(run_id: str, task: EpicRunTask) -> dict[str, Any]:
    return {
        "epic_run_id": run_id,
        "node_id": task.node_id,
        "position": task.position,
        "state": task.state,
        "item_id": task.item_id,
        "run_id": task.run_id,
        "reason": task.reason,
        "admitted_at": task.admitted_at,
        "updated_at": task.updated_at,
    }


class EpicRunStore:
    """Epic runs in the daemon's store. A write is one transaction: the run
    row, the task rows it changes and the events that describe the change."""

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    def get(self, epic_run_id: str) -> EpicRun | None:
        with self.dstore.read() as session:
            row = session.get(PlanEpicRunRow, epic_run_id)
            if row is None:
                return None
            tasks = list(
                session.scalars(
                    select(PlanEpicRunTaskRow).where(PlanEpicRunTaskRow.epic_run_id == epic_run_id)
                )
            )
            return _run(row, tasks)

    def _ids(self, *conditions: Any) -> list[str]:
        with self.dstore.read() as session:
            return [
                str(i)
                for i in session.scalars(
                    select(PlanEpicRunRow.epic_run_id)
                    .where(*conditions)
                    .order_by(PlanEpicRunRow.created_at.desc(), PlanEpicRunRow.epic_run_id)
                )
            ]

    def latest(self, plan_id: str, node_id: str) -> EpicRun | None:
        """The most recent epic run of one epic."""
        ids = self._ids(PlanEpicRunRow.plan_id == plan_id, PlanEpicRunRow.node_id == node_id)
        return self.get(ids[0]) if ids else None

    def active(self) -> list[EpicRun]:
        """Every epic run the daemon is driving, oldest first."""
        ids = self._ids(PlanEpicRunRow.state == "running")
        return [run for run in (self.get(i) for i in reversed(ids)) if run is not None]

    def create(self, run: EpicRun, *, events: Sequence[PlanEvent], actor: dict[str, Any]) -> None:
        with self.dstore.transaction() as session:
            session.add(
                PlanEpicRunRow(
                    epic_run_id=run.id,
                    plan_id=run.plan_id,
                    node_id=run.node_id,
                    state=run.state,
                    started_by=run.started_by,
                    started_by_display=run.started_by_display,
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                    completed_at=run.completed_at,
                )
            )
            for task in run.tasks:
                session.add(PlanEpicRunTaskRow(**_task_columns(run.id, task)))
            for event in events:
                _event(session, event, run.created_at, actor)

    def save(
        self,
        run: EpicRun,
        *,
        tasks: Sequence[EpicRunTask],
        now: float,
        state: EpicRunState | None = None,
        events: Sequence[PlanEvent] = (),
    ) -> None:
        """Write the tasks that changed, the run's state when it moves, and
        the events that say so."""
        with self.dstore.transaction() as session:
            row = session.get(PlanEpicRunRow, run.id)
            if row is None:
                return
            for task in tasks:
                existing = session.get(PlanEpicRunTaskRow, (run.id, task.node_id))
                columns = _task_columns(run.id, replace(task, updated_at=now))
                if existing is None:
                    session.add(PlanEpicRunTaskRow(**columns))
                else:
                    for key, value in columns.items():
                        setattr(existing, key, value)
            if state is not None and state != row.state:
                row.state = state
                if state in ("completed", "cancelled"):
                    row.completed_at = now
            row.updated_at = now
            for event in events:
                _event(session, event, now, None)

    def adopt(self, item_id: str, epic_run_id: str, profile: str | None) -> bool:
        """Name ``epic_run_id`` as the parent of a queued, unclaimed item
        the admission found already in the queue (a finished row re-queued
        in place keeps the parent it had), with the task's workload
        profile. An item already claimed stays as its claimer left it."""
        values: dict[str, Any] = {
            "parent_item_id": epic_run_id,
            "origin_agent": None,
            "chain_depth": 0,
        }
        if profile is not None:
            values["profile"] = profile
        with self.dstore.transaction() as session:
            result = session.execute(
                update(WorkItemRow)
                .where(
                    WorkItemRow.item_id == item_id,
                    WorkItemRow.state == "queued",
                    WorkItemRow.claimed == 0,
                )
                .values(**values)
            )
            return bool(getattr(result, "rowcount", 0))
