"""The plan driver: a plan whose ``advance`` is ``auto`` moved forward under
an owner's grants, with no person in the step — and a person able to step in
at every one.

Each tick (not while the daemon is held) the driver walks every plan that
may move itself (``advance = auto``, not archived), its nodes in the plan's
order, and for each initiative or epic finds the one step it is at:

1. **break down** — the node has no children yet (or is a root still to be
   generated from its brief): the ``planner`` agent, ``plan.breakdown``.
   Allowed, the breakdown is admitted as any is (:func:`plan_item` and
   :func:`upsert`, binding its agents without memories) and recorded as an
   ``item.admit`` operation. A node below the root waits until it is on the
   forge;
2. **approve** — the node has ``draft`` or ``proposed`` children: the
   ``critic``, ``plan.approve``, judged on ``repository``, ``level`` (the
   children's), ``child_count``, ``proposer`` (the children's
   ``proposed_by`` when they agree; left out when they do not, or one is
   missing — the judge then escalates) and ``review_verdict`` (only while the
   node's review is current for the level as it now reads);
3. **publish** — the level is all approved and ``[delegation]
   publish_delay_s`` has passed since the approval: the ``critic``,
   ``plan.publish``, on ``repository``, ``level`` and ``child_count``, plus
   the current review's verdict;
4. **run** — an epic on the forge whose tasks are all on the forge and that
   was never run: the ``critic``, ``plan.run``, on ``repository``, ``level``
   (``task``) and ``child_count`` (the tasks).

Every step is judged by :func:`~lantern.daemon.controls.delegation.decide`
against the enabled grants and what each already allowed today, and the
answer goes to the decisions ledger with the facts it was judged on and the
plan, node and item or epic run it was about. An allowed step is taken in
this process, as the agent — its principal (``Principal.for_agent``) holds
``items:create`` and nothing else, and no route is involved — through
:func:`~lantern.daemon.controls.operations.record_plan_operation`, so the
operations log names the agent as the actor and the node records it as
``approved_by``, ``published_by`` or the run's ``started_by``.

**At most one act per plan per tick, and one forge write per tick overall**
(publishing a level and starting an epic run are the forge writes; neither
is tried while the daemon's forge has not been provisioned). The plan is
read again before each step, so a person's edit, a flip of ``advance`` back
to ``manual`` or an archive is seen before the next one.

**Fail closed.** Anything the driver cannot settle goes to a person as one
``escalate`` row and the plan is left where it is: no grant covers the act;
the review is missing or stale where a grant asks for one; the reviewer said
``escalate``; a breakdown already ran for the node and left no level (it is
never queued again by the driver); the repository is unknown, disabled or
cannot hold a plan; the act itself was refused or failed (a stale revision,
a forge error). A failed act is tried again ``[daemon] poll_interval_s``
after its first failure, twice as long after each failure in a row and
never more than an hour apart; the count lives in ``daemon_state``, so a
restart neither forgets nor restarts it, and it is cleared when the act
succeeds or the situation moves on. A ``deny`` (self-approval) is
recorded the same way. The driver never approves a level its approving
agent proposed: :func:`decide` denies it.

**Written once.** The same answer to the same situation is not written
again: the newest decision on the node and action is the reference, and a
fresh ``escalate`` or ``deny`` with the same reason and the same facts —
the level's digest among them, so a child edited, added or removed is a new
situation, and the reason names the grants, so a grant change that matters
is too — leaves it standing. The ledger is the memory, so a restart repeats
nothing. An escalation is resolved once: ``acted`` when the step happened
(the driver's own act, or a person's on any surface), ``superseded`` when
the situation it asked about changed.

With no enabled grant the driver takes no step. With no open escalation
either it reads no plan and writes nothing, exactly as before it existed;
with some open (an owner removed or disabled the grants) it still closes
those whose step a person has since taken. A ``manual`` plan is never
touched.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from lantern.daemon.controls.delegation import Decision, Grant, decide
from lantern.daemon.controls.delegation_store import DecisionRecord, DelegationStore, Resolution
from lantern.daemon.controls.intake import PlanAdmission, plan_item, target_key, upsert
from lantern.daemon.controls.operations import OperationSpec, record_plan_operation
from lantern.daemon.controls.principal import Principal
from lantern.daemon.controls.results import ControlError
from lantern.errors import LanternError
from lantern.log import get_logger
from lantern.plans.hierarchy import repository_planning_for
from lantern.plans.model import Plan, PlanNode, child_level, review_digest, review_is_current
from lantern.plans.service import PlanRefusal
from lantern.plans.store import PlanStore
from lantern.vcs.protocol import IssueOps

log = get_logger(__name__)

#: Who proposes and breaks down, and who approves, publishes and starts.
PLANNER = "planner"
CRITIC = "critic"
#: The attribution a breakdown the driver admits is queued under.
BREAKDOWN_BY = "the planner agent (plan driver)"

#: The longest a failed act waits before it is tried again (unless the poll
#: interval itself is longer).
RETRY_CEILING_S = 3600.0
#: ``daemon_state`` prefix of a target's run of failed attempts.
FAILURE_PREFIX = "plan_driver.failing:"

#: The steps that write to the forge: one per tick, across every plan.
FORGE_ACTIONS: frozenset[str] = frozenset({"plan.publish", "plan.run"})

#: The operation each step is recorded as.
OPERATION_FOR: dict[str, str] = {
    "plan.breakdown": "item.admit",
    "plan.approve": "plan.approve",
    "plan.publish": "plan.publish",
    "plan.run": "plan.run",
}


@dataclass(frozen=True, slots=True)
class Step:
    """The one step a node is at, with the facts it is judged on."""

    action: str
    agent: str
    node_id: str
    revision: int
    attrs: dict[str, Any] = field(default_factory=dict)
    #: Not before this (a publish waits out the hold window).
    due_at: float | None = None
    #: A reason the host escalates on before any grant is looked at.
    host: str | None = None

    @property
    def forge(self) -> bool:
        return self.action in FORGE_ACTIONS


class _ActFailed(Exception):
    """An allowed act that did not happen: why, and its operation."""

    def __init__(self, detail: str, operation_id: str | None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.operation_id = operation_id


class PlanDriver:
    """Moves the plans that advance themselves; one per loop."""

    def __init__(self, loop: Any) -> None:
        self.loop = loop
        self.store = PlanStore(loop.dstore)
        # One pass at a time: a tick that overruns is not doubled by the next.
        self._lock = threading.Lock()
        # Per pass: the enabled grants, and whether the forge write is spent.
        self._grants: list[Grant] = []
        self._forge_spent = False
        #: The plan the last forge write went to: the next pass starts after
        #: it, so one plan's steps never starve another's.
        self._forge_last: str | None = None

    @property
    def delegation(self) -> DelegationStore:
        store: DelegationStore = self.loop.delegation
        return store

    # -- the pass --------------------------------------------------------------

    def tick(self, now: float) -> None:
        """One pass over every plan that advances itself. Skipped while a
        pass is under way; one plan that fails is logged and the others
        still move."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._grants = self.delegation.grants(enabled_only=True)
            if not self._grants:
                # Nothing may be taken; what was escalated may still have
                # been taken by a person since, and is closed as such. With
                # nothing open this reads no plan and writes nothing.
                if self.delegation.unresolved_count():
                    self._resolve_only(now)
                return
            self._forge_spent = False
            for plan in _after(self.store.advancing(), self._forge_last):
                try:
                    self._advance(plan.id, now)
                except Exception:
                    log.warning("plan_driver.plan_failed", plan=plan.id, exc_info=True)
        finally:
            self._lock.release()

    def _resolve_only(self, now: float) -> None:
        """The resolution pass alone, over every plan that advances itself:
        what a person settled is closed although no grant lets the driver
        act any more."""
        for plan in self.store.advancing():
            try:
                self._resolve(plan, now)
            except Exception:
                log.warning("plan_driver.resolve_failed", plan=plan.id, exc_info=True)

    def _advance(self, plan_id: str, now: float) -> None:
        """At most one act on one plan: the escalations it no longer
        stands at resolved, then each node's step in the plan's order,
        judged on the plan as it is read right then."""
        plan = self._auto(plan_id)
        if plan is None:
            return
        self._resolve(plan, now)
        for node_id in _order(plan):
            plan = self._auto(plan_id)
            if plan is None:
                return
            node = plan.node(node_id)
            if node is None:
                continue
            step = self._step(plan, node)
            if step is None or (step.due_at is not None and now < step.due_at):
                continue
            if step.forge and not self._forge_open():
                continue
            if self._take(plan, node, step, now):
                return

    def _auto(self, plan_id: str) -> Plan | None:
        """The plan as stored now, while it still advances itself."""
        plan = self.store.get(plan_id)
        if plan is None or plan.archived or plan.advance != "auto":
            return None
        return plan

    def _forge_open(self) -> bool:
        """Whether this pass may write to the forge: not yet this tick, and
        the daemon's forge is there and was provisioned (a reading is never
        what boots its sandbox)."""
        github = getattr(self.loop, "github", None)
        if self._forge_spent or github is None:
            return False
        return bool(getattr(github, "provisioned", True))

    # -- where a node stands ---------------------------------------------------

    def _step(self, plan: Plan, node: PlanNode) -> Step | None:
        """The step ``node`` is at, or ``None`` when it is at none the
        driver takes (a task, a level below a node not yet on the forge, a
        breakdown under way, a re-plan waiting, an epic already run)."""
        level = child_level(node.level)
        if level is None:
            return None
        if node.id != plan.root_id and not node.followed:
            return None
        if node.replan is not None or self.loop.dstore.plan_generations(node.id):
            return None
        children = plan.children(node.id)
        scope: dict[str, Any] = {"repository": node.repository, "level": level}
        host = self._repository_problem(node.repository)
        if not children or (plan.generation_pending and node.id == plan.root_id):
            ran = self.loop.dstore.plan_breakdowns(node.id)
            if ran and host is None:
                last = ran[-1]
                host = (
                    f"a breakdown of {node.title} already ran ({last.item_id}, {last.state}) "
                    f"and left no {level} level; the driver does not queue another, so a "
                    "person breaks it down again or drafts the level"
                )
            return Step("plan.breakdown", PLANNER, node.id, plan.revision, scope, host=host)
        pending = [c for c in children if c.state in ("draft", "proposed")]
        approved = [c for c in children if c.state == "approved"]
        if pending or approved:
            attrs = {**scope, "child_count": len(children)}
            if pending:
                proposers = sorted({c.proposed_by or "" for c in pending})
                if len(proposers) == 1 and proposers[0]:
                    attrs["proposer"] = proposers[0]
                else:
                    # Recorded, never judged: the judge escalates without a
                    # proposer it can name.
                    attrs["proposers"] = [p or None for p in proposers]
            review = node.review if review_is_current(plan, node) else None
            if review is not None:
                attrs["review_verdict"] = review.verdict
            attrs["level_digest"] = review_digest(
                plan, node, include_node=node.state != "published"
            )
            if host is None and review is not None and review.verdict == "escalate":
                host = "the reviewer escalated this level" + (
                    f": {' '.join(review.reasons)}" if review.reasons else ""
                )
            if pending:
                return Step("plan.approve", CRITIC, node.id, plan.revision, attrs, host=host)
            approved_at = max(c.updated_at for c in approved)
            delay = float(self.loop.config.delegation.publish_delay_s)
            return Step(
                "plan.publish",
                CRITIC,
                node.id,
                plan.revision,
                attrs,
                due_at=approved_at + delay,
                host=host,
            )
        if node.level != "epic" or not node.followed:
            return None
        if any(c.state != "published" for c in children):
            return None
        tasks = [c for c in children if c.followed]
        if not tasks or self.loop.epic_runs.runs.latest(plan.id, node.id) is not None:
            return None
        attrs = {"repository": node.repository, "level": "task", "child_count": len(tasks)}
        return Step("plan.run", CRITIC, node.id, plan.revision, attrs, host=host)

    def _repository_problem(self, repo: str) -> str | None:
        """Why ``repo`` cannot hold the level this server would write, or
        ``None`` when it can."""
        config = self.loop.config
        entry = config.find_repo(repo)
        if entry is None:
            return f"{repo} is not a repository configured on this server"
        if not entry.enabled:
            return f"{entry.repo} is disabled on this server"
        planning = repository_planning_for(config, entry.repo)
        if not planning.supported:
            return planning.reason or f"{entry.repo}'s forge can't hold plans"
        return None

    # -- judging and acting ----------------------------------------------------

    def _take(self, plan: Plan, node: PlanNode, step: Step, now: float) -> bool:
        """Judge ``step`` and, allowed, take it. ``True`` when an act was
        attempted (the plan's one for this tick)."""
        key = (plan.id, node.id, step.action)
        latest = self.delegation.latest(action=step.action, plan_id=plan.id, node_id=node.id)
        if step.host is not None:
            decision = Decision(outcome="escalate", reason=step.host)
        else:
            day_start = self.loop.usage_pool.day(now)[0]
            decision = decide(
                self._grants,
                agent_slug=step.agent,
                action=step.action,
                attrs=step.attrs,
                used_today=self.delegation.used_today(day_start),
            )
        if decision.outcome != "allow":
            self._note(decision, plan, node, step, latest, now)
            return False
        # A failed act waits before it is tried again, twice as long after
        # each failure in a row (see :meth:`retry_at`), counted in the
        # daemon's state so a restart neither forgets nor resets it.
        failing = self._failures(key)
        if failing is not None and now < self.retry_at(*failing):
            return False
        if step.forge:
            self._forge_spent = True
            self._forge_last = plan.id
        try:
            operation_id, refs = self._act(plan, node, step, now)
        except _ActFailed as failed:
            log.warning(
                "plan_driver.act_failed",
                plan=plan.id,
                node=node.id,
                action=step.action,
                error=failed.detail,
                failures=(0 if failing is None else failing[0]) + 1,
            )
            self._failed(key, failing, now)
            self._note(
                Decision(
                    outcome="escalate",
                    reason=f"{decision.reason}, but it did not happen: {failed.detail}",
                ),
                plan,
                node,
                step,
                latest,
                now,
                operation_id=failed.operation_id,
            )
            return True
        self._clear(key)
        self.delegation.record(
            decision,
            agent_slug=step.agent,
            action=step.action,
            attrs=step.attrs,
            now=now,
            plan_id=plan.id,
            node_id=node.id,
            repository=node.repository,
            operation_id=operation_id,
            **refs,
        )
        if latest is not None and latest.unresolved:
            self._close(latest, by=f"agent:{step.agent}", resolution="acted", now=now)
        log.info(
            "plan_driver.acted",
            plan=plan.id,
            node=node.id,
            action=step.action,
            agent=step.agent,
            grant=decision.grant_id,
            operation=operation_id,
        )
        return True

    # -- backing off a failed act ---------------------------------------------

    def retry_at(self, failures: int, last: float) -> float:
        """When an act that failed ``failures`` times in a row, the last at
        ``last``, may be tried again: ``[daemon] poll_interval_s`` after the
        first failure, doubling with each one after it, never more than
        :data:`RETRY_CEILING_S` (or the poll interval, if that is longer)."""
        poll = float(self.loop.config.daemon.poll_interval_s)
        ceiling = max(RETRY_CEILING_S, poll)
        wait = poll * (2.0 ** min(max(failures - 1, 0), 32))
        return last + min(wait, ceiling)

    def _failures(self, key: tuple[str, str, str]) -> tuple[int, float] | None:
        """``(failures in a row, when the last was)`` for one target, or
        ``None`` when its last attempt did not fail (or none was made)."""
        raw = self.loop.dstore.get_value(_failure_key(key))
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return int(data["failures"]), float(data["last"])
        except (ValueError, TypeError, KeyError):
            # Unreadable is never a reason to wait forever: tried, and the
            # count starts again from this failure.
            return None

    def _failed(
        self, key: tuple[str, str, str], before: tuple[int, float] | None, now: float
    ) -> None:
        count = 1 if before is None else before[0] + 1
        self.loop.dstore.set_value(_failure_key(key), json.dumps({"failures": count, "last": now}))

    def _clear(self, key: tuple[str, str, str]) -> None:
        if self.loop.dstore.get_value(_failure_key(key)) is not None:
            self.loop.dstore.set_value(_failure_key(key), None)

    def _note(
        self,
        decision: Decision,
        plan: Plan,
        node: PlanNode,
        step: Step,
        latest: DecisionRecord | None,
        now: float,
        *,
        operation_id: str | None = None,
    ) -> None:
        """Record an ``escalate`` or ``deny`` — unless the newest decision
        on this node and action already says the same about the same facts.
        An earlier escalation still open about something else is
        superseded by it."""
        if (
            latest is not None
            and latest.outcome == decision.outcome
            and latest.reason == decision.reason
            and latest.attrs == step.attrs
        ):
            return
        if latest is not None and latest.unresolved:
            self._close(latest, by=None, resolution="superseded", now=now)
        self.delegation.record(
            decision,
            agent_slug=step.agent,
            action=step.action,
            attrs=step.attrs,
            now=now,
            plan_id=plan.id,
            node_id=node.id,
            repository=node.repository,
            operation_id=operation_id,
        )
        log.info(
            "plan_driver.judged",
            plan=plan.id,
            node=node.id,
            action=step.action,
            outcome=decision.outcome,
            reason=decision.reason,
        )

    def _act(
        self, plan: Plan, node: PlanNode, step: Step, now: float
    ) -> tuple[str, dict[str, Any]]:
        """Take ``step`` as its agent, recorded as an operation; the
        operation's id and the refs the decision names. :class:`_ActFailed`
        when it was refused, failed or (a publish) left part of the level
        off the forge."""
        principal = Principal.for_agent(step.agent)
        actor = principal.audit()
        loop = self.loop
        request: dict[str, Any] = {
            "plan_id": plan.id,
            "node_id": node.id,
            "expected_revision": step.revision,
        }
        target: tuple[str, str] = ("plan", plan.id)
        call: Callable[[], Any]
        result: Callable[[Any], dict[str, Any]]
        if step.action == "plan.breakdown":
            admission = PlanAdmission(plan.id, node.id, expected_revision=step.revision)
            key = target_key(admission)
            target = ("item", key)
            request = {"form": "plan", **request}

            def admit() -> Any:
                try:
                    item = plan_item(loop, admission, item_id=key)
                    stored, _ = upsert(loop, item, by=BREAKDOWN_BY)
                except ControlError as exc:
                    raise PlanRefusal(409, exc.code, exc.message) from exc
                return stored

            call, result = admit, lambda item: {"item": {"item_id": item.item_id}}
        elif step.action == "plan.approve":
            request["node_ids"] = None

            def approve() -> Any:
                return loop.plans.approve(
                    plan.id,
                    node.id,
                    expected_revision=step.revision,
                    node_ids=None,
                    now=now,
                    actor=actor,
                )

            call, result = approve, lambda approved: {"revision": approved.revision}
        elif step.action == "plan.publish":
            forge = loop.github

            def publish() -> Any:
                return loop.plans.publish(
                    plan.id,
                    node.id,
                    expected_revision=step.revision,
                    forge_kind=None if forge is None else str(forge.kind),
                    connect=self._connect,
                    clock=loop.clock,
                    actor=actor,
                )

            call = publish
            result = lambda level: {  # noqa: E731
                "results": [r.as_dict() for r in level.results],
                "revision": level.plan.revision,
            }
        else:

            def start() -> Any:
                return loop.epic_runs.start(
                    plan.id, node.id, expected_revision=step.revision, actor=actor, now=now
                )

            call, result = start, lambda run: {"epic_run_id": run.id}
        spec = OperationSpec(
            action=OPERATION_FOR[step.action],
            target_kind=target[0],
            target_key=target[1],
            principal=principal,
            request=request,
            expected_revision=step.revision,
        )
        try:
            operation_id, value = record_plan_operation(
                loop.operations,
                spec,
                call=call,
                result=result,
                clock=loop.clock,
                generation=getattr(loop, "generation", None),
            )
        except PlanRefusal as exc:
            raise _ActFailed(exc.detail, exc.extra.get("operation_id")) from exc
        except Exception as exc:
            raise _ActFailed(f"{type(exc).__name__}: {exc}", None) from exc
        if step.action == "plan.breakdown":
            return operation_id, {"item_id": value.item_id}
        if step.action == "plan.run":
            return operation_id, {"epic_run_id": value.id}
        if step.action == "plan.publish" and value.failed:
            failed = [r for r in value.results if r.outcome == "failed"]
            raise _ActFailed(
                "the forge did not take "
                + "; ".join(f"{r.node_id} ({r.error or 'no reason given'})" for r in failed),
                operation_id,
            )
        return operation_id, {}

    def _connect(self) -> IssueOps:
        """The daemon's forge, as a publish writes to it."""
        github = self.loop.github
        try:
            ops: IssueOps = github.ops()
        except LanternError as exc:
            note = getattr(github, "note_failure", None)
            if callable(note):
                note(exc)
            raise
        return ops

    # -- resolving what was escalated ------------------------------------------

    def _resolve(self, plan: Plan, now: float) -> None:
        """Close each open escalation about ``plan`` whose node has moved to
        another step: ``acted`` when what it asked about happened — whoever
        took it — and ``superseded`` when the level changed under it. One
        still at the same step is left for the judgement to compare."""
        for escalation in self.delegation.unresolved_for_plan(plan.id):
            if escalation.action not in OPERATION_FOR:
                # Another judge's (triage's ``plan.run.retry``): its own to close.
                continue
            node = plan.node(escalation.node_id or "")
            if node is None:
                self._close(escalation, by=None, resolution="superseded", now=now)
                continue
            step = self._step(plan, node)
            if step is not None and step.action == escalation.action:
                continue
            done, by = self._effect(plan, node, escalation.action)
            self._close(escalation, by=by, resolution="acted" if done else "superseded", now=now)
            # What it was about moved on: a run of failures on it is history.
            self._clear((plan.id, node.id, escalation.action))

    def _effect(self, plan: Plan, node: PlanNode, action: str) -> tuple[bool, str | None]:
        """Whether ``action``'s effect stands on ``node``, and who it is
        recorded as taken by."""
        children = plan.children(node.id)
        if action == "plan.breakdown":
            return bool(children or self.loop.dstore.plan_generations(node.id)), None
        if action == "plan.approve":
            done = bool(children) and all(c.state in ("approved", "published") for c in children)
            return done, _newest(c.approved_by for c in _by_update(children))
        if action == "plan.publish":
            done = (
                node.state == "published"
                and bool(children)
                and all(c.state == "published" for c in children)
            )
            return done, _newest(c.published_by for c in _by_update(children))
        if action == "plan.run":
            run = self.loop.epic_runs.runs.latest(plan.id, node.id)
            return run is not None, None if run is None else run.started_by
        return False, None

    def _close(
        self, escalation: DecisionRecord, *, by: str | None, resolution: Resolution, now: float
    ) -> None:
        self.delegation.resolve(escalation.id, by=by, resolution=resolution, now=now)
        log.info(
            "plan_driver.resolved",
            decision=escalation.id,
            action=escalation.action,
            resolution=resolution,
            by=by,
        )


def _failure_key(key: tuple[str, str, str]) -> str:
    """The ``daemon_state`` key one target's run of failures is kept under."""
    plan_id, node_id, action = key
    return f"{FAILURE_PREFIX}{plan_id}:{node_id}:{action}"


def _order(plan: Plan) -> list[str]:
    """The nodes a step can be at, the root first and each parent before
    its children, in the plan's order."""

    def walk(node_id: str) -> Iterator[str]:
        node = plan.node(node_id)
        if node is None or child_level(node.level) is None:
            return
        yield node_id
        for child in plan.children(node_id):
            yield from walk(child.id)

    return list(walk(plan.root_id))


def _after(plans: list[Plan], last: str | None) -> list[Plan]:
    """``plans`` in their order, starting after ``last`` (round robin)."""
    at = next((i for i, plan in enumerate(plans) if plan.id == last), None)
    if at is None:
        return plans
    return plans[at + 1 :] + plans[: at + 1]


def _by_update(nodes: list[PlanNode]) -> list[PlanNode]:
    return sorted(nodes, key=lambda n: (n.updated_at, n.id), reverse=True)


def _newest(values: Iterable[str | None]) -> str | None:
    return next((v for v in values if v), None)


__all__ = ["CRITIC", "PLANNER", "PlanDriver", "Step"]
