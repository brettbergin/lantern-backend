"""Triage: the ``operator`` agent picks a failure back up under an owner's
grants, with no person in the step — and a person able to step in at every
one.

Each tick (not while the daemon is held) triage looks at the work that
stopped in the last :data:`WINDOW_S` and, for each piece, at the one act
that would pick it up again:

* ``run.grant_rounds`` — the item failed because its run spent every fix
  round it had (``runs.exhausted`` is set): :data:`GRANT_ROUNDS` more
  rounds on the same pull request, through the loop's grant-rounds path;
* ``plan.run.retry`` — otherwise, for a task of an epic run whose item
  failed or was blocked (or that was never admitted): the epic run's own
  retry (:meth:`~lantern.daemon.epicruns.EpicRunDriver.retry`);
* ``item.retry`` — otherwise, for any other item that is ``failed`` (its
  attempts spent) or ``blocked``: the loop's item retry, a fresh plan.

A ``plan`` item (a plan level being proposed) is never triaged: the plan
driver and a person own those. A ``cancelled`` item, an item a person
abandoned or dismissed, and deleted work are never touched — a dismissal
is a person's "leave it".

**The failure cause.** :func:`classify` maps the failure to one name of
:data:`~lantern.daemon.controls.delegation.FAILURE_CAUSES`, from the
structured facts first (the run's state, the budget it exhausted, a task
whose verify commands failed) and the recorded reason's wording only as a
last resort; anything it does not recognise is ``unknown``. That name is
the ``failure_cause`` a grant's ``causes`` is compared with. A failure whose
cause is ``needs_person`` or ``unknown`` always goes to a person: triage
writes the escalation itself and never asks the judge.

**Judged, recorded, once.** Every act is judged by
:func:`~lantern.daemon.controls.delegation.decide` on ``repository``,
``failure_cause`` and ``retries`` — how many times the ledger says this
act was already allowed on this target (never the item's attempt count,
which a retry resets) — and the answer goes to the decisions ledger with
the facts and the item, run or epic run it was about. Triage never takes
the same act on one target more than :data:`MAX_RETRIES` times, whatever a
grant's ``max_retries`` says. An allowed act runs in this process as
``Principal.for_agent("operator")`` through
:func:`~lantern.daemon.controls.operations.record_plan_operation`, so the
operations log names the agent as the actor.

**At most one act per target per tick, and :data:`MAX_ACTS` per tick
overall.** A failed act is an escalation naming why.

**Written once per situation.** The newest decision on the target and
action is the reference: a fresh ``escalate`` or ``deny`` with the same
reason and the same facts — the target's state and when it last moved
among them, so a new failure is a new situation — is not written again.
The ledger is the memory, so a restart repeats nothing.

**Resolved by what happens next.** An open escalation is closed when its
target moves on: ``acted`` when the work is under way again (a person
retried it, or granted rounds, on any surface), ``declined`` when a person
dismissed or abandoned it, ``superseded`` when it failed again differently,
was deleted or is gone.

Triage only weighs the actions the operator holds an enabled grant for.
With no enabled ``operator`` grant on any of the three, the tick reads
nothing past the grants and writes nothing.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select

from lantern.daemon.controls.delegation import (
    FAILURE_CAUSES,
    NEEDS_PERSON_CAUSE,
    UNKNOWN_CAUSE,
    Decision,
    Grant,
    decide,
)
from lantern.daemon.controls.delegation_store import DecisionRecord, DelegationStore, Resolution
from lantern.daemon.controls.operations import OperationSpec, record_plan_operation
from lantern.daemon.controls.principal import Principal
from lantern.daemon.model import WorkItem
from lantern.db.daemon_models import DecisionRow
from lantern.errors import LanternError
from lantern.log import get_logger
from lantern.plans.epicrun import EpicRun, EpicRunTask
from lantern.plans.service import PlanRefusal

log = get_logger(__name__)

#: The agent triage acts as.
OPERATOR = "operator"
#: The acts triage takes, in the order a target is matched to one.
TRIAGE_ACTIONS: tuple[str, ...] = ("run.grant_rounds", "plan.run.retry", "item.retry")
#: How many fix rounds one ``run.grant_rounds`` act grants.
GRANT_ROUNDS = 2
#: The most acts in one tick, across every target.
MAX_ACTS = 3
#: The most times one act is taken on one target, whatever a grant says.
MAX_RETRIES = 3
#: How far back a failure is looked at: older work stays with a person.
WINDOW_S = 86400.0

_ROUNDS_CAUSE = {"review": "review_rounds_exhausted", "ci": "ci_rounds_exhausted"}
_ROUNDS_CAUSES = frozenset(_ROUNDS_CAUSE.values())

# -- the failure cause ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureFacts:
    """What is known about one failure: the item's and the run's state,
    the budget the run exhausted, whether a task failed its verify
    commands, and the reason recorded (the run's, else the item's)."""

    item_state: str | None = None
    run_state: str | None = None
    exhausted: str | None = None
    verify_failed: bool = False
    reason: str | None = None


def _any(*patterns: str) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


# Wording that says a person must look: checked first among the texts, so a
# timeout that waited on a maintainer's approval is the approval.
_NEEDS_PERSON = _any(
    r"maintainer to approve",
    r"changes-requested",
    r"only they can dismiss",
    r"a human (?:has|needs) to",
    r"human review threads",
    r"review threads (?:unreconciled|could not be read|were not all read)",
    r"identity could not be resolved",
    r"\bpermission",
    r"not permitted",
    r"\b403\b",
    r"forbidden",
    r"nothing to deliver",
    r"protection rule",
    r"approving review",
    r"merge method",
    r"merge queue",
    r"would not let the loop finish",
)
_CI_TIMEOUT = _any(r"ci_timeout_s\s*=", r"ci did not report within")
_MERGE_CONFLICT = _any(r"conflicts? with (?:its|the) base", r"merge conflict", r"conflict markers")
_SANDBOX = _any(
    r"sandbox (?:disk|memory) exhausted",
    r"out of memory",
    r"no space left on device",
    r"\boom\b",
)
_THROTTLE = _any(
    r"rate[- ]limit",
    r"\b429\b",
    r"overloaded",
    r"usage limit",
    r"quota",
    r"too many requests",
)
_FORGE = _any(r"github", r"forge", r"\bapi\b", r"https?://", r"\bhttp\b")
_TRANSIENT = _any(
    r"\b50[0234]\b",
    r"bad gateway",
    r"service unavailable",
    r"gateway time-?out",
    r"internal server error",
    r"connection (?:reset|refused|aborted)",
    r"temporar(?:y|ily) (?:failure|unavailable)",
    r"name resolution",
    r"timed out",
)
_VERIFY = _any(r"verify command failed", r"verify failed")


def classify(facts: FailureFacts) -> str:
    """The cause of one failure, one of
    :data:`~lantern.daemon.controls.delegation.FAILURE_CAUSES`.

    Structured facts first: a run held by its provider is
    ``provider_throttle``; a run that exhausted its review or CI fix rounds
    is ``review_rounds_exhausted`` / ``ci_rounds_exhausted``; a task whose
    verify commands failed is ``verify_failed``. Then the recorded reason:
    a person must look (``needs_person``) before a CI timeout, a merge
    conflict, the sandbox running out of disk or memory, a provider's
    throttle and a transient forge error. Anything else is ``unknown`` —
    including a ``blocked`` run whose reason says nothing recognisable.
    """
    if facts.run_state == "provider_held":
        return "provider_throttle"
    if facts.exhausted in _ROUNDS_CAUSE:
        return _ROUNDS_CAUSE[facts.exhausted]
    if facts.verify_failed:
        return "verify_failed"
    text = (facts.reason or "").strip()
    if not text:
        return UNKNOWN_CAUSE
    if _NEEDS_PERSON.search(text):
        return NEEDS_PERSON_CAUSE
    if _CI_TIMEOUT.search(text):
        return "ci_timeout"
    if _MERGE_CONFLICT.search(text):
        return "merge_conflict"
    if _SANDBOX.search(text):
        return "sandbox_resource"
    if _THROTTLE.search(text):
        return "provider_throttle"
    if _TRANSIENT.search(text) and _FORGE.search(text):
        return "forge_transient"
    if _VERIFY.search(text):
        return "verify_failed"
    return UNKNOWN_CAUSE


assert set(_ROUNDS_CAUSE.values()) <= set(FAILURE_CAUSES)  # nosec B101 - the closed set


# -- what triage acts on --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """One failure and the one act that would pick it up, with the facts
    it is judged on and the refs the ledger names."""

    action: str
    repository: str
    cause: str
    #: The facts the judge reads, plus the situation (the target's state
    #: and when it last moved), so a new failure is a new situation.
    situation: dict[str, Any]
    item: WorkItem | None = None
    run_id: str | None = None
    epic: EpicRun | None = None
    task: EpicRunTask | None = None
    refs: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.action}:{sorted(self.refs.items())}"


class _ActFailed(Exception):
    def __init__(self, detail: str, operation_id: str | None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.operation_id = operation_id


class Triage:
    """Picks failures back up as the ``operator`` agent; one per loop."""

    def __init__(self, loop: Any) -> None:
        self.loop = loop
        # One pass at a time: a tick that overruns is not doubled by the next.
        self._lock = threading.Lock()

    @property
    def delegation(self) -> DelegationStore:
        store: DelegationStore = self.loop.delegation
        return store

    # -- the pass ----------------------------------------------------------------------

    def tick(self, now: float) -> None:
        """One pass. Skipped while a pass is under way; one target that
        fails is logged and the others still go."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            grants = [
                g
                for g in self.delegation.grants(agent_slug=OPERATOR, enabled_only=True)
                if g.action in TRIAGE_ACTIONS
            ]
            if not grants:
                return
            self._pass(grants, now)
        finally:
            self._lock.release()

    def _pass(self, grants: Sequence[Grant], now: float) -> None:
        self._resolve(now)
        delegated = {g.action for g in grants}
        acts = 0
        for target in self._targets(now):
            if target.action not in delegated:
                continue
            try:
                acted = self._take(target, grants, now, may_act=acts < MAX_ACTS)
            except Exception:
                log.warning("triage.target_failed", target=target.key, exc_info=True)
                continue
            acts += int(acted)

    # -- the candidates ----------------------------------------------------------------

    def _targets(self, now: float) -> Iterator[Target]:
        """Each failure of the window once, matched to its one act."""
        dstore = self.loop.dstore
        since = now - WINDOW_S
        for epic in self.loop.epic_runs.runs.active():
            for task in epic.tasks:
                if task.state != "failed":
                    continue
                target = self._task_target(epic, task, since)
                if target is not None:
                    yield target
        for item in dstore.items(("failed", "blocked")):
            # An epic run's task is retried through its epic run, above; a
            # plan item belongs to the plan driver and a person.
            if item.updated_at < since or item.from_epic_run or item.kind == "plan":
                continue
            target = self._item_target(item)
            if target is not None:
                yield target

    def _left_alone(self, item: WorkItem) -> bool:
        """A person said leave it (dismissed, abandoned), or it was deleted."""
        dstore = self.loop.dstore
        if dstore.work_mark("item", item.item_id, "dismissed") is not None:
            return True
        if dstore.work_mark("item", item.item_id, "deleted") is not None:
            return True
        if item.run_id and dstore.work_mark("run", item.run_id, "deleted") is not None:
            return True
        if item.pending_report == "abandoned":
            return True
        return (item.last_error or "").lower().startswith("abandoned")

    def _facts(self, item: WorkItem | None, reason: str | None = None) -> tuple[FailureFacts, Any]:
        record = None
        verify_failed = False
        if item is not None and item.run_id:
            try:
                record = self.loop.store.get_run(item.run_id)
            except LanternError:
                record = None
            if record is not None:
                try:
                    verify_failed = any(
                        t.state == "failed" and t.verify_fingerprints
                        for t in self.loop.store.get_tasks(item.run_id)
                    )
                except LanternError:
                    verify_failed = False
        facts = FailureFacts(
            item_state=None if item is None else item.state,
            run_state=None if record is None else str(record.state),
            exhausted=None if record is None else record.exhausted,
            verify_failed=verify_failed,
            reason=(None if record is None else record.reason)
            or (None if item is None else item.last_error)
            or reason,
        )
        return facts, record

    def _repository(self, repo: str | None) -> str:
        return repo or self.loop.config.primary_repo or ""

    def _item_target(self, item: WorkItem) -> Target | None:
        if self._left_alone(item):
            return None
        facts, record = self._facts(item)
        cause = classify(facts)
        situation = {"state": item.state, "since": item.updated_at}
        repository = self._repository(item.repo)
        if (
            item.state == "failed"
            and cause in _ROUNDS_CAUSES
            and record is not None
            and item.run_id is not None
        ):
            return Target(
                "run.grant_rounds",
                repository,
                cause,
                situation,
                item=item,
                run_id=item.run_id,
                refs={"run_id": item.run_id, "item_id": item.item_id},
            )
        return Target(
            "item.retry",
            repository,
            cause,
            situation,
            item=item,
            run_id=item.run_id,
            refs={"item_id": item.item_id},
        )

    def _task_target(self, epic: EpicRun, task: EpicRunTask, since: float) -> Target | None:
        dstore = self.loop.dstore
        item = dstore.get(task.item_id) if task.item_id else None
        if item is not None:
            if item.state not in ("failed", "blocked") or self._left_alone(item):
                return None
            if item.updated_at < since:
                return None
        elif task.updated_at < since:
            return None
        plan = self.loop.epic_runs.plans.get(epic.plan_id)
        node = plan.node(task.node_id) if plan is not None else None
        repository = self._repository(
            node.repository if node is not None else (item.repo if item else None)
        )
        facts, record = self._facts(item, task.reason)
        cause = classify(facts)
        refs = {"plan_id": epic.plan_id, "node_id": task.node_id, "epic_run_id": epic.id}
        if item is not None:
            refs["item_id"] = item.item_id
        situation = {
            "state": item.state if item is not None else task.state,
            "since": item.updated_at if item is not None else task.updated_at,
        }
        if (
            item is not None
            and item.state == "failed"
            and cause in _ROUNDS_CAUSES
            and record is not None
            and item.run_id is not None
        ):
            return Target(
                "run.grant_rounds",
                repository,
                cause,
                situation,
                item=item,
                run_id=item.run_id,
                refs={"run_id": item.run_id, "item_id": item.item_id},
            )
        return Target(
            "plan.run.retry",
            repository,
            cause,
            situation,
            item=item,
            run_id=None if item is None else item.run_id,
            epic=epic,
            task=task,
            refs=refs,
        )

    # -- the ledger --------------------------------------------------------------------

    def _where(self, action: str, refs: dict[str, Any]) -> list[Any]:
        """The ledger rows about ``action`` on this target."""
        clauses: list[Any] = [DecisionRow.action == action]
        if action == "run.grant_rounds":
            clauses.append(DecisionRow.run_id == refs["run_id"])
        elif action == "plan.run.retry":
            clauses += [
                DecisionRow.epic_run_id == refs["epic_run_id"],
                DecisionRow.node_id == refs["node_id"],
            ]
        else:
            clauses.append(DecisionRow.item_id == refs["item_id"])
        return clauses

    def _latest(self, target: Target) -> DecisionRecord | None:
        stmt = (
            select(DecisionRow.decision_id)
            .where(*self._where(target.action, target.refs))
            .order_by(DecisionRow.at.desc(), DecisionRow.decision_id.desc())
            .limit(1)
        )
        with self.delegation.dstore.read() as session:
            decision_id = session.scalar(stmt)
        return None if decision_id is None else self.delegation.decision(str(decision_id))

    def _retries(self, target: Target) -> int:
        """How many times this act was already allowed on this target."""
        stmt = (
            select(func.count())
            .select_from(DecisionRow)
            .where(*self._where(target.action, target.refs), DecisionRow.outcome == "allow")
        )
        with self.delegation.dstore.read() as session:
            return int(session.scalar(stmt) or 0)

    # -- judging and acting ------------------------------------------------------------

    def _take(self, target: Target, grants: Sequence[Grant], now: float, *, may_act: bool) -> bool:
        """Judge ``target`` and, allowed and within this tick's budget,
        act. ``True`` when an act was attempted."""
        retries = self._retries(target)
        attrs = {
            "repository": target.repository,
            "failure_cause": target.cause,
            "retries": retries,
            **target.situation,
        }
        latest = self._latest(target)
        host: str | None = None
        if target.cause == NEEDS_PERSON_CAUSE:
            host = "the failure says a person must look at it"
        elif target.cause == UNKNOWN_CAUSE:
            host = "triage could not tell what caused the failure, so a person looks at it"
        elif retries >= MAX_RETRIES:
            host = f"{OPERATOR} already took {target.action} here {retries} times, the most it may"
        elif not target.repository:
            host = "could not tell which repository the work belongs to"
        if host is not None:
            decision = Decision(outcome="escalate", reason=host)
        else:
            day_start = self.loop.usage_pool.day(now)[0]
            decision = decide(
                grants,
                agent_slug=OPERATOR,
                action=target.action,
                attrs=attrs,
                used_today=self.delegation.used_today(day_start),
            )
        if decision.outcome != "allow":
            self._note(decision, target, attrs, latest, now)
            return False
        if not may_act:
            return False
        if (
            latest is not None
            and latest.outcome == "escalate"
            and latest.operation_id is not None
            and latest.attrs == attrs
        ):
            # This very act was tried on this very situation and did not
            # happen: it is not tried again until the situation moves.
            return False
        try:
            operation_id = self._act(target, now)
        except _ActFailed as failed:
            log.warning("triage.act_failed", target=target.key, error=failed.detail)
            self._note(
                Decision(
                    outcome="escalate",
                    reason=f"{decision.reason}, but it did not happen: {failed.detail}",
                ),
                target,
                attrs,
                latest,
                now,
                operation_id=failed.operation_id,
            )
            return True
        self._record(decision, target, attrs, now, operation_id=operation_id)
        if latest is not None and latest.unresolved:
            self._close(latest, by=f"agent:{OPERATOR}", resolution="acted", now=now)
        log.info(
            "triage.acted",
            action=target.action,
            target=target.key,
            cause=target.cause,
            grant=decision.grant_id,
            operation=operation_id,
        )
        return True

    def _record(
        self,
        decision: Decision,
        target: Target,
        attrs: dict[str, Any],
        now: float,
        *,
        operation_id: str | None = None,
    ) -> None:
        self.delegation.record(
            decision,
            agent_slug=OPERATOR,
            action=target.action,
            attrs=attrs,
            now=now,
            repository=target.repository or None,
            operation_id=operation_id,
            **target.refs,
        )

    def _note(
        self,
        decision: Decision,
        target: Target,
        attrs: dict[str, Any],
        latest: DecisionRecord | None,
        now: float,
        *,
        operation_id: str | None = None,
    ) -> None:
        """Record an ``escalate`` or ``deny`` unless the newest decision on
        this target and act already says the same about the same facts."""
        if (
            latest is not None
            and latest.outcome == decision.outcome
            and latest.reason == decision.reason
            and latest.attrs == attrs
        ):
            return
        if latest is not None and latest.unresolved:
            self._close(latest, by=None, resolution="superseded", now=now)
        self._record(decision, target, attrs, now, operation_id=operation_id)
        log.info(
            "triage.judged",
            action=target.action,
            target=target.key,
            outcome=decision.outcome,
            reason=decision.reason,
        )

    def _act(self, target: Target, now: float) -> str:
        """Take the act as the operator agent, recorded as an operation;
        the operation's id. :class:`_ActFailed` when it was refused or
        failed."""
        loop = self.loop
        principal = Principal.for_agent(OPERATOR)
        by = principal.attribution()
        call: Callable[[], Any]
        judged = target.item
        if target.action == "item.retry":
            assert judged is not None  # nosec B101 - an item target names its item
            item_id = judged.item_id
            spec = OperationSpec(
                action="item.retry",
                target_kind="item",
                target_key=item_id,
                principal=principal,
                request={},
                expected_revision=judged.revision,
            )

            def call() -> Any:
                self._unchanged(judged)
                try:
                    return loop.retry_item(item_id, by)
                except (KeyError, ValueError) as exc:
                    raise PlanRefusal(409, "not_eligible", str(exc)) from exc

        elif target.action == "run.grant_rounds":
            run_id = target.run_id
            assert judged is not None and run_id is not None  # nosec B101 - set together
            spec = OperationSpec(
                action="run.grant_rounds",
                target_kind="run",
                target_key=run_id,
                principal=principal,
                request={"rounds": GRANT_ROUNDS},
            )

            def call() -> Any:
                self._unchanged(judged)
                try:
                    return loop.grant_rounds(run_id, GRANT_ROUNDS, by)
                except (KeyError, ValueError) as exc:
                    raise PlanRefusal(409, "not_eligible", str(exc)) from exc

        else:
            epic, task = target.epic, target.task
            assert epic is not None and task is not None  # nosec B101 - a task target
            spec = OperationSpec(
                action="plan.run.retry",
                target_kind="plan",
                target_key=epic.plan_id,
                principal=principal,
                request={"plan_id": epic.plan_id, "node_id": task.node_id},
            )

            def call() -> Any:
                if judged is not None:
                    self._unchanged(judged)
                return loop.epic_runs.retry(
                    epic.plan_id, task.node_id, actor=principal.audit(), now=now
                )

        try:
            operation_id, _ = record_plan_operation(
                loop.operations,
                spec,
                call=call,
                result=lambda _value: {},
                clock=loop.clock,
                generation=getattr(loop, "generation", None),
            )
        except PlanRefusal as exc:
            raise _ActFailed(exc.detail, exc.extra.get("operation_id")) from exc
        except Exception as exc:
            raise _ActFailed(f"{type(exc).__name__}: {exc}", None) from exc
        return operation_id

    def _unchanged(self, item: WorkItem | None) -> None:
        """Refuse when the item moved since it was judged."""
        if item is None:
            return
        now = self.loop.dstore.get(item.item_id)
        if now is None or now.revision != item.revision:
            raise PlanRefusal(
                409, "stale_revision", f"{item.item_id} changed after triage looked at it"
            )

    # -- resolving what was escalated --------------------------------------------------

    def _resolve(self, now: float) -> None:
        """Close each open triage escalation whose target moved on."""
        stmt = (
            select(DecisionRow.decision_id)
            .where(
                DecisionRow.outcome == "escalate",
                DecisionRow.resolved_at.is_(None),
                func.lower(DecisionRow.agent_slug) == OPERATOR,
                DecisionRow.action.in_(TRIAGE_ACTIONS),
            )
            .order_by(DecisionRow.at.asc())
        )
        with self.delegation.dstore.read() as session:
            ids = [str(i) for i in session.scalars(stmt)]
        for decision_id in ids:
            escalation = self.delegation.decision(decision_id)
            if escalation is None or not escalation.unresolved:
                continue
            try:
                outcome = self._moved_on(escalation)
            except Exception:
                log.warning("triage.resolve_failed", decision=decision_id, exc_info=True)
                continue
            if outcome is not None:
                self._close(escalation, by=None, resolution=outcome, now=now)

    def _moved_on(self, escalation: DecisionRecord) -> Resolution | None:
        """How ``escalation`` ended, or ``None`` while its situation stands."""
        dstore = self.loop.dstore
        if escalation.action == "plan.run.retry" and escalation.epic_run_id:
            epic = self.loop.epic_runs.runs.get(escalation.epic_run_id)
            task = epic.task(escalation.node_id or "") if epic is not None else None
            if epic is None or task is None or epic.state in ("cancelled", "completed"):
                return "superseded"
            if task.state in ("queued", "running", "landed", "closed"):
                return "acted"
            if task.state != "failed":
                return "superseded"
        item_id = escalation.item_id
        if not item_id:
            return None
        item = dstore.get(item_id)
        if item is None or dstore.work_mark("item", item.item_id, "deleted") is not None:
            return "superseded"
        if dstore.work_mark("item", item.item_id, "dismissed") is not None:
            return "declined"
        if item.pending_report == "abandoned" or (item.last_error or "").lower().startswith(
            "abandoned"
        ):
            return "declined"
        since = escalation.attrs.get("since")
        if item.state == escalation.attrs.get("state") and item.updated_at == since:
            return None
        if item.state in ("failed", "blocked", "cancelled"):
            return "superseded"
        return "acted"

    def _close(
        self, escalation: DecisionRecord, *, by: str | None, resolution: Resolution, now: float
    ) -> None:
        self.delegation.resolve(escalation.id, by=by, resolution=resolution, now=now)
        log.info(
            "triage.resolved",
            decision=escalation.id,
            action=escalation.action,
            resolution=resolution,
        )
