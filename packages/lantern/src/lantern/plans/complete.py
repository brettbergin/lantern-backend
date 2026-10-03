"""Closing a finished epic, and a finished initiative, with a summary (#2349).

After publish the forge is the record, so "finished" is read from it: an
epic is finished when the issue of every one of its published tasks is
closed on the forge, and an initiative when every one of its published
epics is. A task an epic run **skipped** is done for the run — its
dependents went ahead — but its issue is still open until a person closes
it, and an epic with an open task is not finished: closing it would say on
the forge that work is complete which the forge itself still shows open.
Closing that task later finishes the epic then.

For a finished epic (or initiative) whose repository has ``[planning]
close_completed`` on, lantern comments a summary on its issue — each task
and how it ended (landed, closed, skipped) with a link to its pull request
or delivery where one is known, and the epic run; for an initiative, each
epic — and closes the issue as completed. The comment carries a hidden
marker, ``<!-- sbx-plan-summary: <plan_id>/<node_id> -->``: an attempt that
died between the comment and the close finds its comment and does not write
a second. An issue a person already closed is recorded closed and gets no
comment. With ``close_completed`` off, both are left open.

On every pass the children's states are also written where the forge shows
them: a parent's managed checklist (GitLab, or a GitHub cross-repository
fallback) has each closed child's line ticked — the checklist's own
contract, whatever ``close_completed`` says — and each node whose issue
changed state is recorded ``closed`` (or ``open`` again) with a
``plan.node.changed`` event. A mangled checklist is logged and left as it
is. Nothing here decides *when* to look; the epic-run driver does.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from lantern.config import Config
from lantern.errors import LanternError
from lantern.log import get_logger
from lantern.plans.epicrun import EpicRun
from lantern.plans.forgeread import issue_ref, state_of
from lantern.plans.model import ForgeState, Plan, PlanNode
from lantern.plans.render import issue_reference
from lantern.plans.store import PlanEvent, PlanStore, StaleRevision, retry_stale
from lantern.vcs.checklist import (
    ChecklistMangled,
    parse_checklist,
    set_child_closed,
    update_checklist,
)
from lantern.vcs.protocol import IssueOps

log = get_logger(__name__)

#: Where a task's run left its result, when that is known: a pull request's
#: or a delivery's URL. Given the task and the epic run's task state for it
#: (``None`` when no epic run holds it).
LinkOf = Callable[[PlanNode, str | None], str | None]


def summary_marker(plan_id: str, node_id: str) -> str:
    """The hidden line that marks lantern's completion summary on an issue."""
    return f"<!-- sbx-plan-summary: {plan_id}/{node_id} -->"


@dataclass(slots=True)
class CompletionResult:
    """What one look at a node did."""

    #: Children whose line in the parent's checklist was written.
    ticked: list[str] = field(default_factory=list)
    #: Nodes whose recorded forge state moved.
    recorded: list[str] = field(default_factory=list)
    #: Nodes lantern closed with a summary.
    closed: list[str] = field(default_factory=list)
    #: Children whose issue is still open (what keeps the node open).
    open: list[str] = field(default_factory=list)


def _on_forge(nodes: Sequence[PlanNode]) -> list[PlanNode]:
    """The children a parent's completion is judged by: those on the forge
    and still following their issue. One reconcile detached (it left its
    parent there, or the forge) is left as reconcile left it — it neither
    keeps the parent open nor is ticked, recorded or summarised."""
    return [n for n in nodes if n.followed and n.forge is not None]


class Completion:
    """One look at a plan's nodes on the forge, through ``ops``."""

    def __init__(
        self,
        ops: IssueOps,
        *,
        store: PlanStore,
        config: Config,
        clock: Callable[[], float],
    ) -> None:
        self.ops = ops
        self.store = store
        self.config = config
        self.clock = clock

    # -- epics -----------------------------------------------------------------

    def epic(
        self,
        plan_id: str,
        epic_id: str,
        *,
        epic_run: EpicRun | None = None,
        link: LinkOf | None = None,
    ) -> CompletionResult:
        """Look at one epic: tick and record its tasks as the forge shows
        them, and — when every task is closed and ``close_completed`` is on
        for its repository — summarise and close it, then look at its
        initiative."""
        result = CompletionResult()
        plan = self.store.get(plan_id)
        epic = None if plan is None else plan.node(epic_id)
        if plan is None or epic is None or epic.level != "epic" or epic.forge is None:
            return result
        if epic.state != "published":
            return result
        tasks = _on_forge(plan.children(epic.id))
        states = self._follow(plan, epic, tasks, result)
        result.open = [t.id for t in tasks if states.get(t.id) != "closed"]
        if not tasks or result.open:
            return result
        if self.config.planning_for(epic.repository).close_completed:
            body = self._epic_summary(plan, epic, tasks, epic_run, link)
            self._close(plan.id, epic, body, result)
        if epic.parent_id is not None:
            more = self.initiative(plan_id, epic.parent_id)
            result.ticked += more.ticked
            result.recorded += more.recorded
            result.closed += more.closed
        return result

    def _epic_summary(
        self,
        plan: Plan,
        epic: PlanNode,
        tasks: Sequence[PlanNode],
        epic_run: EpicRun | None,
        link: LinkOf | None,
    ) -> str:
        counts: dict[str, int] = {}
        lines: list[str] = []
        for task in tasks:
            assert task.forge is not None  # nosec B101 - _on_forge
            held = epic_run.task(task.id) if epic_run is not None else None
            ran = held.state if held is not None else None
            how = ran if ran in ("landed", "skipped") else "closed"
            counts[how] = counts.get(how, 0) + 1
            words = {
                "landed": "landed",
                "skipped": "skipped in the epic run, then closed on the forge",
                "closed": "closed",
            }[how]
            where = None
            if link is not None:
                try:
                    where = link(task, ran)
                except Exception:  # a link is a courtesy, never a failure
                    log.debug("plan_completion.link_failed", task=task.id, exc_info=True)
            ref = issue_reference(epic.repository, task.repository, task.forge.number)
            title = " ".join(task.title.split())
            lines.append(f"- {ref} {title} — {words}" + (f": {where}" if where else ""))
        tally = ", ".join(
            f"{counts[k]} {k}" for k in ("landed", "closed", "skipped") if counts.get(k)
        )
        noun = "task" if len(tasks) == 1 else "tasks"
        if epic_run is not None:
            who = epic_run.started_by_display or epic_run.started_by
            run = f"Epic run `{epic_run.id}`" + (f", started by {who}." if who else ".")
        else:
            run = "No epic run took part: its tasks were closed on the forge."
        return "\n\n".join(
            [
                summary_marker(plan.id, epic.id),
                f"**Every task of this epic is closed**, so lantern is closing it "
                f"({len(tasks)} {noun}: {tally}).",
                "\n".join(lines),
                run,
            ]
        )

    # -- initiatives -----------------------------------------------------------

    def initiative(self, plan_id: str, node_id: str) -> CompletionResult:
        """Look at one initiative: tick and record its epics as the forge
        shows them, and — when every epic is closed and ``close_completed``
        is on for its repository — comment a rollup and close it."""
        result = CompletionResult()
        plan = self.store.get(plan_id)
        node = None if plan is None else plan.node(node_id)
        if plan is None or node is None or node.level != "initiative" or node.forge is None:
            return result
        if node.state != "published":
            return result
        epics = _on_forge(plan.children(node.id))
        states = self._follow(plan, node, epics, result)
        result.open = [e.id for e in epics if states.get(e.id) != "closed"]
        if not epics or result.open:
            return result
        if not self.config.planning_for(node.repository).close_completed:
            return result
        plan = self.store.get(plan_id) or plan
        lines: list[str] = []
        for epic in epics:
            assert epic.forge is not None  # nosec B101 - _on_forge
            tasks = _on_forge(plan.children(epic.id))
            ref = issue_reference(node.repository, epic.repository, epic.forge.number)
            title = " ".join(epic.title.split())
            noun = "task" if len(tasks) == 1 else "tasks"
            lines.append(f"- {ref} {title} — closed, {len(tasks)} {noun}")
        noun = "epic" if len(epics) == 1 else "epics"
        body = "\n\n".join(
            [
                summary_marker(plan.id, node.id),
                f"**Every epic of this initiative is closed**, so lantern is closing it "
                f"({len(epics)} {noun}).",
                "\n".join(lines),
            ]
        )
        self._close(plan.id, node, body, result)
        return result

    # -- the steps -------------------------------------------------------------

    def _follow(
        self,
        plan: Plan,
        parent: PlanNode,
        children: Sequence[PlanNode],
        result: CompletionResult,
    ) -> dict[str, ForgeState]:
        """Each child's issue state as the forge has it now; the parent's
        checklist ticked to match and the states that moved recorded."""
        states: dict[str, ForgeState] = {}
        for child in children:
            assert child.forge is not None  # nosec B101 - _on_forge
            states[child.id] = state_of(self.ops.issue_get(child.repository, child.forge.number))
        if children:
            self._tick(parent, children, states, result)
        moved = [
            c
            for c in children
            if c.forge is not None and c.forge.state != states.get(c.id, c.forge.state)
        ]
        for child in moved:
            state = states[child.id]
            if self._record(
                plan.id, child.id, state, "closed" if state == "closed" else "reopened"
            ):
                result.recorded.append(child.id)
        return states

    def _tick(
        self,
        parent: PlanNode,
        children: Sequence[PlanNode],
        states: Mapping[str, ForgeState],
        result: CompletionResult,
    ) -> None:
        """Tick (or untick) each child's line in the parent's managed
        checklist, when it has one: one read, one write at most. A parent
        with no block is left alone; a mangled one is logged, not repaired."""
        assert parent.forge is not None  # nosec B101 - checked by the callers

        moved: list[str] = []

        def change(body: str) -> str:
            listed = {entry.ref: entry.closed for entry in parse_checklist(body)}
            moved.clear()
            for child in children:
                closed = states[child.id] == "closed"
                if listed.get(issue_ref(child), closed) != closed:
                    moved.append(child.id)
                body = set_child_closed(body, issue_ref(child), closed=closed)
            return body

        try:
            if update_checklist(self.ops, parent.repository, parent.forge.number, change):
                result.ticked += moved
        except ChecklistMangled as exc:
            log.warning(
                "plan_completion.checklist_mangled",
                plan=parent.plan_id,
                node=parent.id,
                error=str(exc),
                hint="a person broke the managed children block; fix it on the forge",
            )

    def _close(self, plan_id: str, node: PlanNode, body: str, result: CompletionResult) -> None:
        """Comment ``body`` on the node's issue unless its summary is
        already there, then close it as completed; an issue already closed
        (a person did it) is only recorded."""
        assert node.forge is not None  # nosec B101 - checked by the callers
        repo, number = node.repository, node.forge.number
        if state_of(self.ops.issue_get(repo, number)) == "closed":
            if self._record(plan_id, node.id, "closed", "closed"):
                result.recorded.append(node.id)
            return
        mark = summary_marker(plan_id, node.id)
        comments = self.ops.issue_comments(repo, number)
        if not any(isinstance(c, dict) and mark in str(c.get("body") or "") for c in comments):
            self.ops.issue_comment(repo, number, body)
        self.ops.issue_close(repo, number, reason="completed")
        log.info("plan_completion.closed", plan=plan_id, node=node.id, level=node.level)
        result.closed.append(node.id)
        if self._record(plan_id, node.id, "closed", "completed"):
            result.recorded.append(node.id)

    def _record(self, plan_id: str, node_id: str, state: ForgeState, change: str) -> bool:
        """The node's forge state written as ``state``, with a
        ``plan.node.changed`` event saying ``change``; whether it moved."""

        def attempt() -> bool:
            plan = self.store.get(plan_id)
            node = None if plan is None else plan.node(node_id)
            if plan is None or node is None or node.forge is None:
                return False
            if node.forge.state == state:
                return False
            now = self.clock()
            # Only the state moves: what reconcile knew of the issue (its
            # version, a missing marker, a checklist it could not read)
            # stays on record.
            forge = replace(node.forge, state=state)
            self.store.apply(
                plan.id,
                expected_revision=plan.revision,
                now=now,
                upsert=[replace(node, forge=forge, updated_at=now)],
                events=[
                    PlanEvent(
                        "plan.node.changed",
                        {
                            "plan_id": plan.id,
                            "node_id": node.id,
                            "change": change,
                            "number": node.forge.number,
                        },
                    )
                ],
            )
            return True

        try:
            return retry_stale(attempt)
        except StaleRevision:
            log.warning("plan_completion.record_failed", plan=plan_id, node=node_id, state=state)
            return False


def complete(
    connect: Callable[[], IssueOps | None],
    *,
    store: PlanStore,
    config: Config,
    clock: Callable[[], float],
    plan_id: str,
    epic_id: str,
    epic_run: EpicRun | None = None,
    link: LinkOf | None = None,
) -> CompletionResult | None:
    """Look at one epic through the forge ``connect`` opens; ``None`` when
    there is no forge or it could not be reached (the next look retries)."""
    try:
        ops = connect()
        if ops is None:
            return None
        return Completion(ops, store=store, config=config, clock=clock).epic(
            plan_id, epic_id, epic_run=epic_run, link=link
        )
    except LanternError as exc:
        log.warning("plan_completion.forge_failed", plan=plan_id, epic=epic_id, error=str(exc))
        return None


__all__ = ["Completion", "CompletionResult", "LinkOf", "complete", "summary_marker"]
