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
2. admits every task whose dependencies are all ``landed``, ``closed`` or ``skipped``
   through the same issue admission ``POST /v1/items`` uses
   (:func:`~sbxloop.daemon.controls.intake.admit_issue`) — but with no
   queueing label, so no poll-driven path is added — with
   ``parent_item_id`` naming the epic run, a code task as a code run and a
   workload task as a workload run under its ``workload_profile``. A task
   whose dependency failed or is blocked is ``blocked`` (the reason names
   which) and is not admitted; every task it does not lead to goes on;
3. completes the run when every task is ``landed``, ``closed`` or
   ``skipped``.

Every task's move records one ``plan.run.task_*`` event, and a task that
fails records ``plan.run.paused`` with ``reason: "task_failed"``: the run
goes on for everything else, but cannot complete until a person acts.

A person controls the run (#2348, each a ``plans:publish`` route):
``pause`` stops admission (what is queued or running goes on, followed);
``resume`` admits the ready set again; ``cancel`` stops it for good —
nothing more is admitted, an item still waiting in the queue is withdrawn
through the item abandon, and a run under way is left to finish and
followed; ``retry`` re-queues a failed task's item through the item retry
(or admits it afresh when it has none) and its dependents wait on it again;
``skip`` treats a task as done without touching its issue.

Nothing here decides how many run at once: an admitted task is a queued
item like any other, held back by the queue, the holds and the usage pool.

Completion (#2349, :mod:`sbxloop.plans.complete`) is looked at through the
daemon's forge whenever a pass sees a task land or close and when the run
completes; again every :data:`SWEEP_S` for :data:`SWEEP_WINDOW_S` after a
run completed while its epic is still open (a forge hiccup, or a skipped
task a person closes later); and when the source reports an issue of a
plan's task closed outside any live epic run (:meth:`issue_closed`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from sbxloop.daemon.controls.intake import IssueAdmission, admit_issue, upsert
from sbxloop.daemon.controls.results import ControlError
from sbxloop.daemon.model import WorkItem
from sbxloop.daemon.store import TERMINAL_ITEM_STATES
from sbxloop.errors import ConfigError, SbxloopError
from sbxloop.log import get_logger
from sbxloop.plans.complete import CompletionResult, complete
from sbxloop.plans.epicrun import (
    DONE,
    LIVE_RUN_STATES,
    SETTLED,
    EpicRun,
    EpicRunState,
    EpicRunStore,
    EpicRunTask,
    TaskState,
    blocked_by,
    dependents,
    from_item,
    new_epic_run_id,
    readiness,
)
from sbxloop.plans.model import Plan, PlanNode
from sbxloop.plans.service import PlanRefusal
from sbxloop.plans.store import PlanEvent, PlanStore
from sbxloop.vcs.protocol import IssueOps

log = get_logger(__name__)

#: How often completed runs whose epic is still open are looked at again.
SWEEP_S = 600.0
#: For how long after a run completed its epic is looked at again.
SWEEP_WINDOW_S = 14 * 86400.0


class EpicRunDriver:
    """Starts epic runs and drives the running ones; one per loop."""

    def __init__(self, loop: Any) -> None:
        self.loop = loop
        self.runs = EpicRunStore(loop.dstore)
        self.plans = PlanStore(loop.dstore)
        # One pass at a time: a start from the API and the loop's tick
        # would otherwise both admit the same ready task.
        self._lock = threading.Lock()
        # One completion look at a time: the tick and a source's report
        # would otherwise both find an epic finished and comment twice.
        self._completing = threading.Lock()
        self._last_sweep: float | None = None
        #: Opens the forge completion reads and writes: the daemon's own.
        self.forge: Callable[[], IssueOps | None] = self._loop_forge

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
            # Recorded is started: the loop's next pass admits what this
            # one could not.
            self._drive_quietly(run.id, now)
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
        """Pass over every live epic run once. Skipped while a start or a
        control holds the pass (the next tick catches up); one run that
        fails is logged and the others still move."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            for run in self.runs.active():
                try:
                    self._drive(run.id, now)
                except Exception:
                    log.warning("epic_run.drive_failed", epic_run=run.id, exc_info=True)
            if self._last_sweep is None or now - self._last_sweep >= SWEEP_S:
                self._last_sweep = now
                self._sweep(now)
        finally:
            self._lock.release()

    def _drive_quietly(self, epic_run_id: str, now: float) -> None:
        """A pass right after a start or a control. The change is recorded
        already: a pass that fails here is the loop's next tick's to make."""
        try:
            self._drive(epic_run_id, now)
        except Exception:
            log.warning("epic_run.drive_failed", epic_run=epic_run_id, exc_info=True)

    def _drive(self, epic_run_id: str, now: float) -> None:
        """One pass: follow what was admitted; while the run is running,
        admit what is ready; while it is paused, only say what is ready; a
        stopped run's in-flight tasks are followed to their end."""
        run = self.runs.get(epic_run_id)
        if run is None or run.state == "completed":
            return
        plan = self.plans.get(run.plan_id)
        if plan is None or plan.node(run.node_id) is None:
            log.warning("epic_run.plan_gone", epic_run=run.id, plan=run.plan_id)
            return
        order = [n for n in plan.children(run.node_id) if run.task(n.id) is not None]
        before = {t.node_id: t for t in run.tasks}
        tasks = dict(before)
        changed: dict[str, EpicRunTask] = {}

        def put(task: EpicRunTask) -> None:
            if tasks.get(task.node_id) != task:
                tasks[task.node_id] = task
                changed[task.node_id] = task

        for task in list(tasks.values()):
            put(self._follow(task))
        if run.state in LIVE_RUN_STATES:
            self._place(run, plan, order, tasks, put, now, admit=run.state == "running")
        events = self._events(run, order, before, tasks, changed)
        state: EpicRunState | None = None
        if run.state in LIVE_RUN_STATES and all(t.state in DONE for t in tasks.values()):
            state = "completed"
            events.append(
                PlanEvent(
                    "plan.run.completed",
                    {
                        **_ids(run),
                        "landed": [k for k, t in tasks.items() if t.state == "landed"],
                        "closed": [k for k, t in tasks.items() if t.state == "closed"],
                        "skipped": [k for k, t in tasks.items() if t.state == "skipped"],
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
        closing = any(
            t.state in ("landed", "closed") and before[k].state != t.state
            for k, t in changed.items()
            if k in before
        )
        if closing or state == "completed":
            # A task closed: its line ticked, its epic closed when it was
            # the last. Recorded first, so a forge that fails here only
            # delays that to the next look.
            self._complete(run.plan_id, run.node_id, now)

    def _place(
        self,
        run: EpicRun,
        plan: Plan,
        order: list[PlanNode],
        tasks: dict[str, EpicRunTask],
        put: Callable[[EpicRunTask], None],
        now: float,
        *,
        admit: bool,
    ) -> None:
        """Where each task not yet admitted stands — and, when ``admit``,
        the ready ones admitted — in the plan's order until nothing moves:
        a task found closed can make its dependents ready, and a failure
        blocks its dependents' dependents, within the one pass. Only a
        failed or blocked task's dependents are held back: a sibling it
        does not lead to is admitted as usual."""
        tried: set[str] = set()
        for _ in range(len(order) + 1):
            moved = False
            states = {k: t.state for k, t in tasks.items()}
            for node in order:
                task = tasks[node.id]
                if task.item_id is not None or task.state in SETTLED or task.state == "failed":
                    continue
                if node.forge is not None and node.forge.state == "closed":
                    put(replace(task, state="closed", reason=None))
                    moved = True
                    continue
                wanted = readiness(node, states)
                if wanted == "ready" and node.id in tried:
                    # Tried on this pass: its answer (the forge could not
                    # be read, say) stands until the next one.
                    continue
                if wanted == "ready" and admit:
                    tried.add(node.id)
                    admitted = self._admit(run, node, task, now)
                    put(admitted)
                    moved = moved or admitted.state != task.state
                    continue
                reason = _blocked_reason(plan, node, states) if wanted == "blocked" else None
                if (wanted, reason) != (task.state, task.reason):
                    put(replace(task, state=wanted, reason=reason))
                    moved = True
            if not moved:
                break

    def _events(
        self,
        run: EpicRun,
        order: list[PlanNode],
        before: Mapping[str, EpicRunTask],
        tasks: Mapping[str, EpicRunTask],
        changed: Mapping[str, EpicRunTask],
    ) -> list[PlanEvent]:
        """One event per task whose state moved, in the plan's order, and a
        ``plan.run.paused`` notice for each task that newly failed while
        the run is live: the run cannot complete until a person retries or
        skips it, and its dependents wait for that."""
        states = {k: t.state for k, t in tasks.items()}
        nodes = {n.id: n for n in order}
        events: list[PlanEvent] = []
        failed: list[EpicRunTask] = []
        for key in sorted(changed, key=lambda k: (changed[k].position, k)):
            task = changed[key]
            prior = before.get(key)
            if prior is not None and (prior.state, prior.admitted_at) == (
                task.state,
                task.admitted_at,
            ):
                continue  # a reason or a run id moved, not the state
            node = nodes.get(key)
            extra: dict[str, Any] = {}
            if task.state == "blocked" and node is not None:
                extra["blocked_by"] = blocked_by(node, states)
            events.append(_task_event(run, prior, task, **extra))
            if task.state == "failed" and (prior is None or prior.state != "failed"):
                failed.append(task)
        if run.state in LIVE_RUN_STATES:
            for task in failed:
                events.append(
                    PlanEvent(
                        "plan.run.paused",
                        {
                            **_ids(run),
                            "reason": "task_failed",
                            "state": run.state,
                            "task_node_id": task.node_id,
                            "item_id": task.item_id,
                            "error": task.reason,
                            "blocked": [
                                k
                                for k in dependents(order, task.node_id)
                                if states.get(k) == "blocked"
                            ],
                        },
                    )
                )
        return events

    def _follow(self, task: EpicRunTask) -> EpicRunTask:
        """``task`` as its item now stands. ``landed``, ``closed``,
        ``skipped`` and ``cancelled`` are final; an item that left the
        queue (a claim that failed forgets its row) makes the task ready to
        be admitted again."""
        if task.item_id is None or task.state in SETTLED:
            return task
        item = self.loop.dstore.get(task.item_id)
        seen = from_item(item)
        if seen is None or item is None:
            return replace(
                task, state="ready", item_id=None, reason="its item left the queue; admitting again"
            )
        state, reason = seen
        return replace(task, state=state, reason=reason, run_id=item.run_id or task.run_id)

    # -- completion (#2349) ----------------------------------------------------

    def _loop_forge(self) -> IssueOps | None:
        """The daemon's forge, or ``None`` when it has none."""
        github = getattr(self.loop, "github", None)
        if github is None:
            return None
        try:
            ops: IssueOps = github.ops()
        except SbxloopError as exc:
            note = getattr(github, "note_failure", None)
            if callable(note):
                note(exc)
            raise
        return ops

    def _complete(self, plan_id: str, epic_id: str, now: float) -> CompletionResult | None:
        """Look at one epic's completion; never raises."""
        run = self.runs.latest(plan_id, epic_id)

        def link(task: PlanNode, ran: str | None) -> str | None:
            held = run.task(task.id) if run is not None else None
            run_id = held.run_id if held is not None and held.run_id else None
            if run_id is None and task.forge is not None:
                run_id = self._last_run(task.repository, task.forge.number)
            return self._result_link(run_id) if run_id else None

        try:
            with self._completing:
                return complete(
                    self.forge,
                    store=self.plans,
                    config=self.loop.config,
                    clock=lambda: now,
                    plan_id=plan_id,
                    epic_id=epic_id,
                    epic_run=run,
                    link=link,
                )
        except Exception:
            log.warning("epic_run.completion_failed", plan=plan_id, epic=epic_id, exc_info=True)
            return None

    def _sweep(self, now: float) -> None:
        """Look again at each recently completed run's epic that is still
        open where ``close_completed`` would close it."""
        seen: set[tuple[str, str]] = set()
        for run in self.runs.completed_since(now - SWEEP_WINDOW_S):
            key = (run.plan_id, run.node_id)
            if key in seen:
                continue
            seen.add(key)
            plan = self.plans.get(run.plan_id)
            epic = None if plan is None else plan.node(run.node_id)
            if epic is None or epic.forge is None or epic.forge.state == "closed":
                continue
            if not self.loop.config.planning_for(epic.repository).close_completed:
                continue
            self._complete(run.plan_id, run.node_id, now)

    def issue_closed(self, repo: str, number: int, now: float) -> None:
        """The source closed issue ``number`` of ``repo`` (a run landed or
        delivered): when it is a plan's task outside a live epic run (a
        live run's own pass looks), look at its epic."""
        for plan_id, task in self.plans.published_at(repo, number):
            if task.level != "task" or task.parent_id is None:
                continue
            held = self.runs.for_task(plan_id, task.id)
            if held is not None and held.state in LIVE_RUN_STATES:
                continue
            self._complete(plan_id, task.parent_id, now)

    def _last_run(self, repo: str, number: int) -> str | None:
        """The run of the newest finished item for issue ``number``."""
        wanted = repo.casefold()
        found = [
            item
            for item in self.loop.dstore.items()
            if item.source_key == str(number)
            and (item.repo or "").casefold() == wanted
            and item.state == "done"
            and item.run_id
        ]
        found.sort(key=lambda item: item.updated_at)
        return found[-1].run_id if found else None

    def _result_link(self, run_id: str) -> str | None:
        """Where a run's result is: its pull request, else the first
        delivery that is a web address."""
        report = self.loop.report_for(run_id)
        if report.pr is not None and report.pr[1]:
            return str(report.pr[1])
        for published in report.published:
            if str(published.location).startswith(("https://", "http://")):
                return str(published.location)
        return None

    # -- controls (#2348) ------------------------------------------------------

    def run_for(self, plan_id: str, node_id: str) -> EpicRun:
        """The run a control on ``node_id`` addressed: the epic's latest, or
        the latest that holds the task."""
        run = self.runs.latest(plan_id, node_id) or self.runs.for_task(plan_id, node_id)
        if run is None:
            raise PlanRefusal(404, "not_found", f"{node_id} of plan {plan_id} has not been run")
        return run

    def pause(self, plan_id: str, node_id: str, *, actor: Mapping[str, Any], now: float) -> EpicRun:
        """Stop admitting: what is queued or running goes on and is still
        followed; nothing new is admitted until the run is resumed."""
        with self._lock:
            run = self._epic_run(plan_id, node_id)
            _refuse_ended(run)
            if run.state == "paused":
                raise PlanRefusal(
                    409,
                    "already_paused",
                    f"epic run {run.id} is already paused",
                    epic_run_id=run.id,
                )
            self.runs.save(
                run,
                tasks=[],
                now=now,
                state="paused",
                events=[
                    PlanEvent(
                        "plan.run.paused",
                        {**_ids(run), "reason": "person", "state": "paused", "by": _who(actor)},
                    )
                ],
                actor=actor,
            )
            log.info("epic_run.paused", epic_run=run.id, by=_who(actor))
        return self._read(run.id)

    def resume(
        self, plan_id: str, node_id: str, *, actor: Mapping[str, Any], now: float
    ) -> EpicRun:
        """Admit again: the pass made here admits the ready set at once."""
        with self._lock:
            run = self._epic_run(plan_id, node_id)
            _refuse_ended(run)
            if run.state != "paused":
                raise PlanRefusal(
                    409, "not_paused", f"epic run {run.id} is {run.state}", epic_run_id=run.id
                )
            self.runs.save(
                run,
                tasks=[],
                now=now,
                state="running",
                events=[PlanEvent("plan.run.resumed", {**_ids(run), "by": _who(actor)})],
                actor=actor,
            )
            log.info("epic_run.resumed", epic_run=run.id, by=_who(actor))
            self._drive_quietly(run.id, now)
        return self._read(run.id)

    def cancel(
        self, plan_id: str, node_id: str, *, actor: Mapping[str, Any], now: float
    ) -> EpicRun:
        """Stop the run for good. A task not yet admitted never is; a task
        whose item is still waiting in the queue (no run started or
        pinned) has the item withdrawn through the item abandon; a task
        whose run is under way is left to finish — a person cancels that
        run through the run controls — and is followed to its end."""
        with self._lock:
            run = self._epic_run(plan_id, node_id)
            _refuse_ended(run)
            who = _who(actor)
            why = f"the epic run {run.id} was stopped by {who}"
            plan = self.plans.get(run.plan_id)
            order = [] if plan is None else list(plan.children(run.node_id))
            before = {t.node_id: t for t in run.tasks}
            tasks = dict(before)
            changed: dict[str, EpicRunTask] = {}
            withdrawn: list[str] = []
            running: list[str] = []
            for task in run.tasks:
                now_task = self._follow(task)
                if now_task.state in ("waiting", "ready", "blocked"):
                    now_task = replace(now_task, state="cancelled", reason=f"never admitted: {why}")
                elif (
                    now_task.state == "queued"
                    and now_task.item_id is not None
                    and self._withdraw(now_task.item_id, why)
                ):
                    withdrawn.append(task.node_id)
                    now_task = replace(now_task, state="cancelled", reason=f"withdrawn: {why}")
                elif now_task.state in ("queued", "running"):
                    running.append(task.node_id)
                if now_task != task:
                    tasks[task.node_id] = now_task
                    changed[task.node_id] = now_task
            events = self._events(
                replace(run, state="cancelled"),
                [n for n in order if n.id in tasks],
                before,
                tasks,
                changed,
            )
            events.append(
                PlanEvent(
                    "plan.run.cancelled",
                    {**_ids(run), "by": who, "withdrawn": withdrawn, "running": running},
                )
            )
            self.runs.save(
                run,
                tasks=list(changed.values()),
                now=now,
                state="cancelled",
                events=events,
                actor=actor,
            )
            log.info(
                "epic_run.cancelled",
                epic_run=run.id,
                by=who,
                withdrawn=len(withdrawn),
                running=len(running),
            )
        return self._read(run.id)

    def retry(self, plan_id: str, node_id: str, *, actor: Mapping[str, Any], now: float) -> EpicRun:
        """Run a failed task again. An item that failed, was blocked or was
        cancelled is re-queued through the item retry — attempts start
        over, the run is unpinned, the issue hears who asked — and a task
        with no item (its admission was refused, or the row is gone) is
        admitted afresh. Either way its dependents wait on it again. A
        person's retry is theirs to make while the run is paused, too."""
        with self._lock:
            run, node = self._task_run(plan_id, node_id)
            _refuse_ended(run)
            self._drive(run.id, now)
            run = self._read(run.id)
            task = run.task(node.id)
            assert task is not None  # nosec B101 - found by the task above
            if task.state == "blocked":
                plan = self.plans.get(run.plan_id)
                deps = blocked_by(node, {t.node_id: t.state for t in run.tasks})
                names = ", ".join(_title(plan, d) for d in deps)
                raise PlanRefusal(
                    409,
                    "task_blocked",
                    f"{node.title} is blocked by {names}: retry or skip that first",
                    blocked_by=deps,
                )
            if task.state != "failed":
                raise PlanRefusal(
                    409,
                    "task_not_failed",
                    f"{node.title} is {task.state}; only a failed task is retried",
                    state=task.state,
                )
            who = _who(actor)
            item = self.loop.dstore.get(task.item_id) if task.item_id else None
            if item is not None and item.state in ("failed", "blocked", "cancelled"):
                try:
                    fresh: WorkItem = self.loop.retry_item(
                        item.item_id, by=f"{who} (epic run {run.id})"
                    )
                except (KeyError, ValueError) as exc:
                    raise PlanRefusal(409, "not_eligible", str(exc)) from exc
                seen = from_item(fresh)
                state, reason = seen if seen is not None else ("queued", None)
                retried = replace(task, state=state, reason=reason, run_id=fresh.run_id)
                via = "item"
            else:
                retried = self._admit(
                    run, node, replace(task, state="ready", item_id=None, reason=None), now
                )
                via = "admission"
            events = [
                PlanEvent(
                    "plan.run.task_retried",
                    {
                        **_ids(run),
                        "task_node_id": node.id,
                        "from": task.state,
                        "state": retried.state,
                        "item_id": retried.item_id,
                        "via": via,
                        "by": who,
                    },
                )
            ]
            if via == "admission":
                # What the fresh admission came to: admitted, or refused
                # again (failed, with the notice that says so).
                events += self._events(
                    run, [node], {node.id: task}, {node.id: retried}, {node.id: retried}
                )
            self.runs.save(run, tasks=[retried], now=now, events=events, actor=actor)
            log.info("epic_run.task_retried", epic_run=run.id, task=node.id, via=via, by=who)
            self._drive_quietly(run.id, now)
        return self._read(run.id)

    def skip(self, plan_id: str, node_id: str, *, actor: Mapping[str, Any], now: float) -> EpicRun:
        """Treat a task that is not under way as done, so its dependents
        become ready. Its issue is left exactly as it is — open, and with
        whatever label its last run left — and its item is not touched."""
        with self._lock:
            run, node = self._task_run(plan_id, node_id)
            _refuse_ended(run)
            self._drive(run.id, now)
            run = self._read(run.id)
            task = run.task(node.id)
            assert task is not None  # nosec B101 - found by the task above
            if task.state in ("queued", "running"):
                raise PlanRefusal(
                    409,
                    "task_in_progress",
                    f"{node.title} is {task.state}: let it finish, or abandon its item first",
                    state=task.state,
                    item_id=task.item_id,
                )
            if task.state in SETTLED:
                raise PlanRefusal(
                    409, "task_settled", f"{node.title} is already {task.state}", state=task.state
                )
            who = _who(actor)
            skipped = replace(task, state="skipped", reason=f"skipped by {who}")
            self.runs.save(
                run,
                tasks=[skipped],
                now=now,
                events=[
                    PlanEvent(
                        "plan.run.task_skipped",
                        {
                            **_ids(run),
                            "task_node_id": node.id,
                            "from": task.state,
                            "state": "skipped",
                            "item_id": task.item_id,
                            "by": who,
                        },
                    )
                ],
                actor=actor,
            )
            log.info("epic_run.task_skipped", epic_run=run.id, task=node.id, by=who)
            self._drive_quietly(run.id, now)
        return self._read(run.id)

    def _read(self, epic_run_id: str) -> EpicRun:
        run = self.runs.get(epic_run_id)
        assert run is not None  # nosec B101 - runs are never deleted
        return run

    def _epic_run(self, plan_id: str, node_id: str) -> EpicRun:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanRefusal(404, "not_found", f"no plan {plan_id}")
        node = plan.node(node_id)
        if node is None:
            raise PlanRefusal(404, "not_found", f"no node {node_id} in plan {plan_id}")
        if node.level != "epic":
            raise PlanRefusal(
                422,
                "invalid_argument",
                f"an epic's run is paused, resumed or stopped; {node.title} is a {node.level}",
            )
        return self.latest(plan_id, node_id)

    def _task_run(self, plan_id: str, node_id: str) -> tuple[EpicRun, PlanNode]:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise PlanRefusal(404, "not_found", f"no plan {plan_id}")
        node = plan.node(node_id)
        if node is None:
            raise PlanRefusal(404, "not_found", f"no node {node_id} in plan {plan_id}")
        if node.level != "task":
            raise PlanRefusal(
                422,
                "invalid_argument",
                f"a task is retried or skipped; {node.title} is a {node.level}",
            )
        run = self.runs.for_task(plan_id, node_id)
        if run is None:
            raise PlanRefusal(404, "not_found", f"{node.title} is not a task of any epic run")
        return run, node

    def _withdraw(self, item_id: str, why: str) -> bool:
        """Abandon an item still waiting in the queue — no run started or
        pinned — through the item abandon; ``False`` when it is not (a
        dispatch took it: its run is left to finish)."""
        item = self.loop.dstore.get(item_id)
        if item is None or item.state != "queued" or item.run_id is not None:
            return False
        try:
            self.loop.abandon_item(item_id, why, queued_only=True)
        except (KeyError, ValueError):
            return False
        return True

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


def _ids(run: EpicRun) -> dict[str, Any]:
    return {"plan_id": run.plan_id, "node_id": run.node_id, "epic_run_id": run.id}


def _who(actor: Mapping[str, Any]) -> str:
    return str(actor.get("display") or actor.get("id") or "a person")


def _title(plan: Plan | None, node_id: str) -> str:
    node = plan.node(node_id) if plan is not None else None
    return node.title if node is not None else node_id


def _blocked_reason(plan: Plan, node: PlanNode, states: Mapping[str, TaskState]) -> str:
    deps = blocked_by(node, states)
    return "blocked by " + ", ".join(f"{_title(plan, d)} ({states[d]})" for d in deps)


def _refuse_ended(run: EpicRun) -> None:
    if run.state in ("completed", "cancelled"):
        raise PlanRefusal(
            409,
            "run_ended",
            f"epic run {run.id} is {run.state}",
            epic_run_id=run.id,
            state=run.state,
        )


def _task_event(
    run: EpicRun, prior: EpicRunTask | None, task: EpicRunTask, **extra: Any
) -> PlanEvent:
    """The event for one task's move: ``task_admitted`` when it was
    admitted (a new item), ``task_retried`` when a failed task's item was
    re-queued (by a person's retry through the item controls), else
    ``task_<state>``."""
    if (
        task.item_id is not None
        and task.admitted_at is not None
        and (prior is None or prior.admitted_at != task.admitted_at)
    ):
        kind = "task_admitted"
    elif prior is not None and prior.state == "failed" and task.state in ("queued", "running"):
        kind = "task_retried"
    else:
        kind = f"task_{task.state}"
    return PlanEvent(
        f"plan.run.{kind}",
        {
            **_ids(run),
            "task_node_id": task.node_id,
            "from": None if prior is None else prior.state,
            "state": task.state,
            "item_id": task.item_id,
            "run_id": task.run_id,
            "reason": task.reason,
            **extra,
        },
    )


def _by(run: EpicRun) -> str:
    who = run.started_by_display or run.started_by or "a person"
    return f"{who} (epic run {run.id})"
