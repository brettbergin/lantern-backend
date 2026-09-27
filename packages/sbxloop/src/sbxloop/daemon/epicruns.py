"""The daemon's side of an epic run (#2347): admitting an epic's ready tasks
as issue runs, in dependency order, and following them to the end.

A person starts the run (``POST /v1/plans/{id}/nodes/{epic}/run``,
``plans:publish``); the loop drives it on every tick from then on. One
pass:

1. follows each admitted task's item as the queue holds it — ``done`` is
   ``landed`` (a code run merged, and its PR's ``Closes`` and the source's
   merge report closed the issue; a workload delivered, and the source's
   completed report closed it), a failed, blocked or cancelled item is
   ``failed``;
2. admits every task whose dependencies are all ``landed`` or ``closed``
   through the same issue admission ``POST /v1/items`` uses
   (:func:`~sbxloop.daemon.controls.intake.admit_issue`) — but with no
   queueing label, so no poll-driven path is added — with
   ``parent_item_id`` naming the epic run, a code task as a code run and a
   workload task as a workload run under its ``workload_profile``. A task
   whose dependency failed is ``blocked`` and never admitted;
3. completes the run when every task is ``landed`` or ``closed``.

Nothing here decides how many run at once: an admitted task is a queued
item like any other, held back by the queue, the holds and the usage pool.
Pausing, retrying, skipping and stopping belong to #2348; a failed task
already stops its dependents here.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from sbxloop.daemon.controls.intake import IssueAdmission, admit_issue, upsert
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import TERMINAL_ITEM_STATES
from sbxloop.errors import ConfigError
from sbxloop.log import get_logger
from sbxloop.plans.epicrun import (
    DONE,
    EpicRun,
    EpicRunState,
    EpicRunStore,
    EpicRunTask,
    from_item,
    new_epic_run_id,
    readiness,
)
from sbxloop.plans.model import Plan, PlanNode
from sbxloop.plans.service import PlanRefusal
from sbxloop.plans.store import PlanEvent, PlanStore

log = get_logger(__name__)


class EpicRunDriver:
    """Starts epic runs and drives the running ones; one per loop."""

    def __init__(self, loop: Any) -> None:
        self.loop = loop
        self.runs = EpicRunStore(loop.dstore)
        self.plans = PlanStore(loop.dstore)
        # One pass at a time: a start from the API and the loop's tick
        # would otherwise both admit the same ready task.
        self._lock = threading.Lock()

    # -- reads -----------------------------------------------------------------

    def latest(self, plan_id: str, node_id: str) -> EpicRun:
        """The most recent epic run of ``node_id``; ``404`` when it never ran."""
        run = self.runs.latest(plan_id, node_id)
        if run is None:
            raise PlanRefusal(404, "not_found", f"{node_id} of plan {plan_id} has not been run")
        return run

    # -- start -----------------------------------------------------------------

    def start(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        actor: Mapping[str, Any],
        now: float,
    ) -> EpicRun:
        """Start an epic run on a published epic whose tasks are on the
        forge, and admit what is ready at once."""
        with self._lock:
            plan = self.plans.get(plan_id)
            if plan is None:
                raise PlanRefusal(404, "not_found", f"no plan {plan_id}")
            epic, tasks = self._check_start(plan, node_id, expected_revision)
            run = EpicRun(
                id=new_epic_run_id(),
                plan_id=plan.id,
                node_id=epic.id,
                state="running",
                started_by=str(actor.get("id") or "") or None,
                started_by_display=str(actor.get("display") or "") or None,
                created_at=now,
                updated_at=now,
                tasks=tuple(
                    EpicRunTask(node_id=t.id, position=i, state="waiting", updated_at=now)
                    for i, t in enumerate(tasks)
                ),
            )
            self.runs.create(
                run,
                events=[
                    PlanEvent(
                        "plan.run.started",
                        {"plan_id": plan.id, "node_id": epic.id, "epic_run_id": run.id},
                    )
                ],
                actor=dict(actor),
            )
            log.info(
                "epic_run.started",
                epic_run=run.id,
                plan=plan.id,
                epic=epic.id,
                tasks=len(tasks),
                by=run.started_by_display,
            )
            try:
                self._drive(run.id, now)
            except Exception:
                # Recorded is started: the loop's next pass admits what
                # this one could not.
                log.warning("epic_run.drive_failed", epic_run=run.id, exc_info=True)
        started = self.runs.get(run.id)
        assert started is not None  # nosec B101 - written above
        return started

    def _check_start(
        self, plan: Plan, node_id: str, expected_revision: int
    ) -> tuple[PlanNode, list[PlanNode]]:
        if plan.revision != expected_revision:
            raise PlanRefusal(
                409,
                "stale_revision",
                f"the plan changed since it was read; it is at revision {plan.revision}",
                current_revision=plan.revision,
            )
        if plan.archived:
            raise PlanRefusal(409, "plan_archived", "this plan is archived")
        epic = plan.node(node_id)
        if epic is None:
            raise PlanRefusal(404, "not_found", f"no node {node_id} in plan {plan.id}")
        if epic.level != "epic":
            raise PlanRefusal(422, "invalid_argument", f"only an epic runs; {epic.title} is not")
        if epic.state != "published" or epic.forge is None:
            raise PlanRefusal(
                409, "epic_unpublished", f"{epic.title} is not on the forge yet: publish it first"
            )
        tasks = [c for c in plan.children(epic.id) if c.followed]
        if not tasks:
            raise PlanRefusal(
                409,
                "nothing_to_run",
                f"no task of {epic.title} is on the forge: publish its tasks first",
            )
        running = self.runs.latest(plan.id, epic.id)
        if running is not None and running.state in ("running", "paused"):
            raise PlanRefusal(
                409,
                "already_running",
                f"{epic.title} is already running",
                epic_run_id=running.id,
            )
        config = self.loop.config
        entry = config.find_repo(epic.repository)
        if entry is None:
            raise PlanRefusal(
                422,
                "unknown_repository",
                f"{epic.repository} is not a repository configured on this server",
                repository=epic.repository,
            )
        if not entry.enabled:
            raise PlanRefusal(
                409,
                "repository_disabled",
                f"{entry.repo} is disabled on this server",
                repository=entry.repo,
            )
        if not callable(getattr(self.loop.source, "admit", None)):
            raise PlanRefusal(
                503,
                "source_unavailable",
                "this daemon polls no repository, so it cannot admit an epic's tasks",
            )
        return epic, tasks

    # -- the pass --------------------------------------------------------------

    def tick(self, now: float) -> None:
        """Drive every running epic run once. Skipped while a start holds
        the pass (the next tick catches up); one run that fails is logged
        and the others still move."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            for run in self.runs.active():
                try:
                    self._drive(run.id, now)
                except Exception:
                    log.warning("epic_run.drive_failed", epic_run=run.id, exc_info=True)
        finally:
            self._lock.release()

    def _drive(self, epic_run_id: str, now: float) -> None:
        run = self.runs.get(epic_run_id)
        if run is None or run.state != "running":
            return
        plan = self.plans.get(run.plan_id)
        if plan is None or plan.node(run.node_id) is None:
            log.warning("epic_run.plan_gone", epic_run=run.id, plan=run.plan_id)
            return
        order = [n for n in plan.children(run.node_id) if run.task(n.id) is not None]
        tasks = {t.node_id: t for t in run.tasks}
        changed: dict[str, EpicRunTask] = {}
        events: list[PlanEvent] = []

        def put(task: EpicRunTask) -> None:
            if tasks.get(task.node_id) != task:
                tasks[task.node_id] = task
                changed[task.node_id] = task

        for task in list(tasks.values()):
            put(self._follow(task))
        # Admit in the plan's order until a pass changes nothing: a task
        # found closed can make its dependents ready, and a failure blocks
        # its dependents' dependents, within the one pass.
        tried: set[str] = set()
        for _ in range(len(order) + 1):
            moved = False
            states = {k: t.state for k, t in tasks.items()}
            for node in order:
                task = tasks[node.id]
                if task.item_id is not None or task.state in DONE or task.state == "failed":
                    continue
                if node.forge is not None and node.forge.state == "closed":
                    put(replace(task, state="closed", reason=None))
                    moved = True
                    continue
                wanted = readiness(node, states)
                if wanted != "ready" or node.id in tried:
                    if wanted != "ready" and wanted != task.state:
                        put(replace(task, state=wanted, reason=None))
                        moved = True
                    continue
                tried.add(node.id)
                admitted = self._admit(run, node, task, now)
                put(admitted)
                if admitted.item_id is not None:
                    events.append(
                        PlanEvent(
                            "plan.run.task_admitted",
                            {
                                "plan_id": run.plan_id,
                                "node_id": run.node_id,
                                "epic_run_id": run.id,
                                "task_node_id": node.id,
                                "item_id": admitted.item_id,
                            },
                        )
                    )
                moved = moved or admitted.state != task.state
            if not moved:
                break
        state: EpicRunState | None = None
        if all(t.state in DONE for t in tasks.values()):
            state = "completed"
            events.append(
                PlanEvent(
                    "plan.run.completed",
                    {
                        "plan_id": run.plan_id,
                        "node_id": run.node_id,
                        "epic_run_id": run.id,
                        "landed": [k for k, t in tasks.items() if t.state == "landed"],
                        "closed": [k for k, t in tasks.items() if t.state == "closed"],
                    },
                )
            )
            log.info("epic_run.completed", epic_run=run.id, plan=run.plan_id, epic=run.node_id)
        if changed or state is not None:
            self.runs.save(
                run,
                tasks=list(changed.values()),
                now=now,
                state=state,
                events=events,
            )

    def _follow(self, task: EpicRunTask) -> EpicRunTask:
        """``task`` as its item now stands. ``landed`` and ``closed`` are
        final; an item that left the queue (a claim that failed forgets
        its row) makes the task ready to be admitted again."""
        if task.item_id is None or task.state in DONE:
            return task
        item = self.loop.dstore.get(task.item_id)
        seen = from_item(item)
        if seen is None or item is None:
            return replace(
                task, state="ready", item_id=None, reason="its item left the queue; admitting again"
            )
        state, reason = seen
        return replace(task, state=state, reason=reason, run_id=item.run_id or task.run_id)

    def _admit(self, run: EpicRun, node: PlanNode, task: EpicRunTask, now: float) -> EpicRunTask:
        """Admit one ready task; the task as it then stands."""
        assert node.forge is not None  # nosec B101 - only published tasks run
        kind = node.kind or "code"
        profile = node.workload_profile if kind == "workload" else None
        if profile is not None:
            try:
                self.loop.config.workload_profile(profile)
            except ConfigError as exc:
                return replace(task, state="failed", reason=str(exc))
        live = self._live_item(node.repository, node.forge.number)
        if live is None:
            request = IssueAdmission(
                repository=node.repository, number=node.forge.number, run_kind=kind
            )
            try:
                item = admit_issue(self.loop, request, label=False)
                item = item.model_copy(
                    update={
                        "parent_item_id": run.id,
                        "origin_agent": None,
                        "chain_depth": 0,
                        **({"profile": profile} if profile is not None else {}),
                    }
                )
                live, _ = upsert(self.loop, item, by=_by(run))
            except ControlError as exc:
                if exc.detail.get("issue_state") == "closed":
                    return replace(task, state="closed", reason=None)
                if exc.code == "source_unavailable":
                    # The forge could not be read: ready, and tried again
                    # on the next pass.
                    return replace(task, state="ready", reason=exc.message)
                return replace(task, state="failed", reason=exc.message)
        if live.parent_item_id != run.id:
            # Queued already (a person started it alone, or a pass that
            # died before its write), or a finished row re-queued in place.
            self.runs.adopt(live.item_id, run.id, profile)
        stored = self.loop.dstore.get(live.item_id) or live
        seen = from_item(stored)
        state, reason = seen if seen is not None else ("queued", None)
        log.info(
            "epic_run.task_admitted",
            epic_run=run.id,
            task=node.id,
            item=stored.item_id,
            kind=kind,
            profile=profile,
        )
        return replace(
            task,
            state=state,
            reason=reason,
            item_id=stored.item_id,
            run_id=stored.run_id,
            admitted_at=now,
        )

    def _live_item(self, repo: str, number: int) -> WorkItem | None:
        """The queue's live row for issue ``number`` of ``repo``, if any."""
        wanted = repo.casefold()
        items: list[WorkItem] = self.loop.dstore.items()
        for item in items:
            if (
                item.source_key == str(number)
                and (item.repo or "").casefold() == wanted
                and item.state not in TERMINAL_ITEM_STATES
            ):
                return item
        return None


def _by(run: EpicRun) -> str:
    who = run.started_by_display or run.started_by or "a person"
    return f"{who} (epic run {run.id})"
