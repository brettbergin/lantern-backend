"""Durable operations: every mutating control leaves a record before it acts.

A reply that got lost and a command that never ran look the same to a
client; so do a command the daemon claimed and a command it finished. The
record separates them. An :class:`Operation` is accepted (durable
admission), claimed by a daemon generation, and finished with the typed
outcome or the refusal — each transition written with its audit event in
one transaction, and the effect never acknowledged before the row is.

:class:`OperationStore` owns the rows; :class:`OperationRunner` drives one
command through the sequence; :func:`reconcile_operations` runs at recovery
and settles what a dead generation left, from domain evidence, never from
a timeout. The bus is not the transaction coordinator: an event published
to it proves nothing about durability, so nothing here relies on it.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.orm import Session

from lantern.daemon.controls.principal import Principal
from lantern.daemon.controls.results import ControlError, Outcome
from lantern.db.api_models import ApiEventRow, OperationRow
from lantern.db.event_scope import event_channel
from lantern.ids import _token
from lantern.log import get_logger

if TYPE_CHECKING:
    from lantern.daemon.store import DaemonStore

log = get_logger(__name__)

OperationState = Literal["accepted", "running", "reconciling", "succeeded", "failed", "expired"]
TERMINAL_OPERATION_STATES: frozenset[str] = frozenset({"succeeded", "failed", "expired"})

#: The effect each action promises. Success means exactly this, and no
#: more: a cancelled run's source report, a released gate's merge, the
#: process exit after a stop are separate outcomes with their own events.
EFFECTS: dict[str, str] = {
    "daemon.pause": "the hold stands and nothing new is claimed",
    "daemon.release": "the hold is released",
    "run.cancel": "the run reaches the cancelled state",
    "run.cancel_provider": "the parked run is settled as cancelled",
    "run.resume": "the run is admitted to the queue and resumes at the next tick",
    "run.review_resume": "the review wait is re-armed",
    "run.steer": "the instruction is durably handed to the run's input path",
    "run.grant_rounds": "the grant is recorded and the item re-admitted",
    "gate.approve": "the approval is recorded and the gate release committed",
    "item.admit": "one item is durably admitted through its source's rules",
    "item.abandon": "the item is settled as abandoned and the source owed its report",
    "item.retry": "the item is re-queued with attempts reset",
    "item.requeue": "the item is unpinned and re-queued",
    "item.dismiss": "the item's alert is marked dismissed for everyone",
    "item.undismiss": "the item's alert asks for attention again",
    "run.dismiss": "the run's alert is marked dismissed for everyone",
    "run.undismiss": "the run's alert asks for attention again",
    "attention.dismiss_all": "each named alert is marked dismissed, or named as skipped",
    "item.delete": "the item and its runs are hidden and their run directories removed",
    "run.delete": "the run is hidden and its run directory removed",
    "repo.resume": "the repository is polled again from the next tick",
    "repo.labels_sync": "every label the loop applies exists on the repository",
    "daemon.breaker_reset": "the breaker is closed and its failure count is zero",
    "schedule.add": "the schedule exists and fires from the next tick",
    "schedule.remove": "the schedule is gone",
    "schedule.pause": "the schedule's ticks are swallowed",
    "schedule.resume": "the schedule fires again",
    "daemon.stop": "the graceful stop is committed and signalled",
    "daemon.restart": "the restart is committed and signalled",
    "plan.propose": "the draft plan is stored for its goal, its root generated from the brief",
    "plan.approve": "the node's draft and proposed children are approved",
    "plan.publish": "each node of the level is on the forge and recorded, or named as failed",
    "plan.replan.approve": "each approved entry of the re-plan is on the forge, or named as failed",
    "plan.run": "the epic run is recorded and its ready tasks are admitted",
    "plan.run.pause": "the epic run is paused: nothing new is admitted",
    "plan.run.resume": "the epic run is running again and admits its ready tasks",
    "plan.run.cancel": "the epic run is cancelled and its queued items withdrawn",
    "plan.run.retry": "the task is re-queued or admitted afresh",
    "plan.run.skip": "the task is recorded as skipped",
    "grant.create": "the grant is stored and judged from the next decision",
    "grant.update": "the grant holds the requested change",
    "grant.delete": "the grant is gone; what it allowed stays in the ledger",
    "grant.restore_defaults": "every default grant whose agent can act is in place",
    "decision.decline": "the escalation is resolved; a person declined it unless it was already",
    "goal.create": "the goal is stored for its repository",
    "goal.update": "the goal holds the requested change",
    "goal.delete": "the goal is gone; the plans proposed from it keep naming it",
}


class Operation(BaseModel):
    """One accepted command, as the record describes it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    action: str
    target_kind: str
    target_key: str
    state: OperationState
    effect: str
    actor: dict[str, Any]
    request: dict[str, Any]
    accepted_at: float
    expires_at: float | None = None
    claimed_at: float | None = None
    finished_at: float | None = None
    claimed_generation: str | None = None
    expected_revision: int | None = None
    idempotency_scope: str | None = None
    idempotency_key: str | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_OPERATION_STATES


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """What a surface asks to have recorded before the effect."""

    action: str
    target_kind: str
    target_key: str
    principal: Principal
    request: dict[str, Any] = field(default_factory=dict)
    #: ``(scope, key)``: a retry with the same pair and the same request
    #: returns the same operation; a different request is a conflict.
    idempotency: tuple[str, str] | None = None
    expected_revision: int | None = None
    #: Seconds after acceptance an unclaimed command expires rather than
    #: applying stale intent; ``None`` for a command claimed at once.
    ttl_s: float | None = None
    #: The effect completes after the call returns (a cancel honoured at
    #: the run's next boundary): the runner leaves the row ``running`` and
    #: the daemon finishes it when the effect is observed.
    deferred: bool = False


class IdempotencyConflict(Exception):
    """Same idempotency key, different request."""

    def __init__(self, existing: Operation) -> None:
        super().__init__(f"idempotency key already used by {existing.id} with a different request")
        self.existing = existing


class OperationReplay(Exception):
    """Same idempotency key, same request: the caller gets the existing
    operation instead of a second effect."""

    def __init__(self, existing: Operation) -> None:
        super().__init__(f"replay of {existing.id}")
        self.existing = existing


def new_operation_id() -> str:
    return "op_" + _token(16)


def fingerprint(request: dict[str, Any]) -> str:
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _row(row: OperationRow) -> Operation:
    return Operation(
        id=str(row.id),
        action=str(row.action),
        target_kind=str(row.target_kind),
        target_key=str(row.target_key),
        state=row.state,  # type: ignore[arg-type]
        effect=str(row.effect),
        actor=json.loads(row.actor_json),
        request=json.loads(row.request_json or "{}"),
        accepted_at=float(row.accepted_at),
        expires_at=None if row.expires_at is None else float(row.expires_at),
        claimed_at=None if row.claimed_at is None else float(row.claimed_at),
        finished_at=None if row.finished_at is None else float(row.finished_at),
        claimed_generation=None if row.claimed_generation is None else str(row.claimed_generation),
        expected_revision=None if row.expected_revision is None else int(row.expected_revision),
        idempotency_scope=None if row.idempotency_scope is None else str(row.idempotency_scope),
        idempotency_key=None if row.idempotency_key is None else str(row.idempotency_key),
        result=None if row.result_json is None else json.loads(row.result_json),
        error_code=None if row.error_code is None else str(row.error_code),
        error_detail=None if row.error_detail is None else str(row.error_detail),
    )


def _target_refs(kind: str, key: str) -> tuple[str | None, str | None]:
    """The run and item an event row indexes by, from the target."""
    return (key if kind == "run" else None), (key if kind == "item" else None)


class OperationStore:
    """The ``api_operations`` and ``api_events`` rows, written together.

    Every method that changes an operation writes its audit event in the
    same transaction, under the daemon store's lock, so a reader never sees
    a transition without its event or an event without its transition.
    """

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    # -- writes --------------------------------------------------------------------

    def accept(self, spec: OperationSpec, now: float) -> tuple[Operation, bool]:
        """Durably admit a command. Returns the operation and whether it
        was created now (``False``: an idempotent replay of an existing
        one). Raises :class:`IdempotencyConflict` for a reused key with a
        different request."""
        scope, key = spec.idempotency or (None, None)
        digest = fingerprint(spec.request) if spec.idempotency else None
        with self.dstore.transaction() as session:
            if scope is not None:
                existing = session.scalars(
                    select(OperationRow).where(
                        OperationRow.idempotency_scope == scope,
                        OperationRow.idempotency_key == key,
                    )
                ).first()
                if existing is not None:
                    if existing.fingerprint != digest:
                        raise IdempotencyConflict(_row(existing))
                    return _row(existing), False
            op_id = new_operation_id()
            session.execute(
                insert(OperationRow).values(
                    id=op_id,
                    action=spec.action,
                    target_kind=spec.target_kind,
                    target_key=spec.target_key,
                    state="accepted",
                    effect=EFFECTS.get(spec.action, ""),
                    actor_json=json.dumps(spec.principal.audit(), default=str),
                    idempotency_scope=scope,
                    idempotency_key=key,
                    fingerprint=digest,
                    expected_revision=spec.expected_revision,
                    request_json=json.dumps(spec.request, sort_keys=True, default=str),
                    accepted_at=now,
                    expires_at=None if spec.ttl_s is None else now + spec.ttl_s,
                )
            )
            self._event(
                session,
                "operation.accepted",
                now,
                op_id,
                spec.target_kind,
                spec.target_key,
                spec.principal,
                {"action": spec.action, "state": "accepted"},
            )
            row = session.get(OperationRow, op_id)
            assert row is not None  # nosec B101 - just inserted under the lock
            return _row(row), True

    def claim(self, op_id: str, generation: str | None, now: float) -> None:
        with self.dstore.transaction() as session:
            session.execute(
                update(OperationRow)
                .where(OperationRow.id == op_id, OperationRow.state == "accepted")
                .values(state="running", claimed_at=now, claimed_generation=generation)
            )

    def finish(
        self,
        op_id: str,
        now: float,
        *,
        state: Literal["succeeded", "failed", "expired", "reconciling"],
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> Operation | None:
        """Record the terminal (or reconciling) state. A no-op on a row
        that is already terminal: the first verdict stands."""
        with self.dstore.transaction() as session:
            row = session.get(OperationRow, op_id)
            if row is None or row.state in TERMINAL_OPERATION_STATES:
                return None if row is None else _row(row)
            row.state = state
            row.finished_at = now if state in TERMINAL_OPERATION_STATES else None
            row.result_json = None if result is None else json.dumps(result, default=str)
            row.error_code = error_code
            row.error_detail = error_detail
            self._event(
                session,
                "operation.finished"
                if state in TERMINAL_OPERATION_STATES
                else "operation.reconciling",
                now,
                op_id,
                str(row.target_kind),
                str(row.target_key),
                None,
                {
                    "action": str(row.action),
                    "state": state,
                    "error_code": error_code,
                    "error_detail": error_detail,
                },
            )
            session.flush()
            return _row(row)

    # -- reads ---------------------------------------------------------------------

    def get(self, op_id: str) -> Operation | None:
        with self.dstore.read() as session:
            row = session.get(OperationRow, op_id)
            return None if row is None else _row(row)

    def for_idempotency(self, scope: str, key: str) -> Operation | None:
        """The operation an idempotency pair already names, if any: what a
        surface asks before it works out what a replay is a replay of."""
        with self.dstore.read() as session:
            row = session.scalars(
                select(OperationRow).where(
                    OperationRow.idempotency_scope == scope,
                    OperationRow.idempotency_key == key,
                )
            ).first()
            return None if row is None else _row(row)

    def recent(
        self,
        *,
        states: Sequence[str] | None = None,
        target: tuple[str, str] | None = None,
        limit: int = 100,
    ) -> list[Operation]:
        """Newest first, bounded."""
        stmt = select(OperationRow).order_by(OperationRow.accepted_at.desc()).limit(limit)
        if states:
            stmt = stmt.where(OperationRow.state.in_(list(states)))
        if target is not None:
            stmt = stmt.where(
                OperationRow.target_kind == target[0], OperationRow.target_key == target[1]
            )
        with self.dstore.read() as session:
            return [_row(row) for row in session.scalars(stmt)]

    def page(
        self,
        *,
        states: Sequence[str] | None = None,
        target: tuple[str, str] | None = None,
        after: tuple[float, str] | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        """A page newest first, keyed on ``(accepted_at, id)``: ``after`` is
        the last row of the previous page, so a listing never skips or
        repeats a row that landed between two requests."""
        stmt = (
            select(OperationRow)
            .order_by(OperationRow.accepted_at.desc(), OperationRow.id.desc())
            .limit(limit)
        )
        if states:
            stmt = stmt.where(OperationRow.state.in_(list(states)))
        if target is not None:
            stmt = stmt.where(
                OperationRow.target_kind == target[0], OperationRow.target_key == target[1]
            )
        if after is not None:
            accepted_at, op_id = after
            stmt = stmt.where(
                or_(
                    OperationRow.accepted_at < accepted_at,
                    and_(OperationRow.accepted_at == accepted_at, OperationRow.id < op_id),
                )
            )
        with self.dstore.read() as session:
            return [_row(row) for row in session.scalars(stmt)]

    def pending(self) -> list[Operation]:
        """Every operation not yet settled, oldest first."""
        stmt = (
            select(OperationRow)
            .where(OperationRow.state.in_(["accepted", "running", "reconciling"]))
            .order_by(OperationRow.accepted_at.asc())
        )
        with self.dstore.read() as session:
            return [_row(row) for row in session.scalars(stmt)]

    def events(self, *, after_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """The public chronology after a cursor, oldest first."""
        stmt = (
            select(ApiEventRow)
            .where(ApiEventRow.seq > after_seq)
            .order_by(ApiEventRow.seq.asc())
            .limit(limit)
        )
        with self.dstore.read() as session:
            return [
                {
                    "seq": int(row.seq),
                    "recorded_at": float(row.recorded_at),
                    "occurred_at": float(row.occurred_at),
                    "type": str(row.type),
                    "run_id": row.run_id,
                    "item_id": row.item_id,
                    "operation_id": row.operation_id,
                    "actor": None if row.actor_json is None else json.loads(row.actor_json),
                    "source_seq": row.source_seq,
                    "data": {} if row.data_json is None else json.loads(row.data_json),
                    "schema_version": int(row.schema_version),
                }
                for row in session.scalars(stmt)
            ]

    @staticmethod
    def _event(
        session: Session,
        type_: str,
        now: float,
        op_id: str,
        target_kind: str,
        target_key: str,
        principal: Principal | None,
        data: dict[str, Any],
    ) -> None:
        run_id, item_id = _target_refs(target_kind, target_key)
        session.execute(
            insert(ApiEventRow).values(
                recorded_at=now,
                occurred_at=now,
                type=type_,
                run_id=run_id,
                item_id=item_id,
                operation_id=op_id,
                actor_json=None if principal is None else json.dumps(principal.audit()),
                source_seq=None,
                data_json=json.dumps(data, default=str),
                channel_id=event_channel(session, type_, run_id=run_id, item_id=item_id, data=data),
            )
        )


class OperationRunner:
    """Drive one command: accept, claim, apply, finish.

    The four ``after_*`` seams are no-ops a fault-injection test replaces
    to crash the process between steps; each boundary has a defined
    outcome under :func:`reconcile_operations`.
    """

    def __init__(
        self,
        store: OperationStore,
        *,
        generation: Callable[[], str | None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.generation = generation
        self.clock = clock
        self.after_accept: Callable[[Operation], None] = lambda op: None
        self.after_claim: Callable[[Operation], None] = lambda op: None
        self.after_effect: Callable[[Operation], None] = lambda op: None
        self.after_commit: Callable[[Operation], None] = lambda op: None

    def run_sync(self, spec: OperationSpec, fn: Callable[[str], Outcome]) -> Outcome:
        """Record ``spec``, then apply ``fn`` (handed the operation id, for
        an effect that completes later) in the caller's thread.

        Raises :class:`OperationReplay` when the idempotency pair names an
        operation that already exists with the same request, and
        :class:`IdempotencyConflict` when the request differs. A
        :class:`ControlError` from ``fn`` finishes the operation ``failed``
        with its code and is re-raised; any other exception finishes it
        ``failed`` with ``code="crashed"`` and is re-raised too.
        """
        op, created = self.store.accept(spec, self.clock())
        if not created:
            raise OperationReplay(op)
        self.after_accept(op)
        self.store.claim(op.id, self.generation(), self.clock())
        self.after_claim(op)
        try:
            outcome = fn(op.id)
        except ControlError as exc:
            self.store.finish(
                op.id, self.clock(), state="failed", error_code=exc.code, error_detail=exc.message
            )
            # The refusal names its record too: the surface answers with
            # the sentence and the id a client can look up.
            exc.detail.setdefault("operation_id", op.id)
            raise
        except Exception as exc:
            self.store.finish(
                op.id,
                self.clock(),
                state="failed",
                error_code="crashed",
                error_detail=f"{type(exc).__name__}: {exc}"[:2000],
            )
            raise
        self.after_effect(op)
        after = getattr(outcome, "after", None)
        if callable(after):
            # The effect runs once the reply is on its way (a stop must not
            # tear a chat bridge down under its own answer): the row stays
            # running until then, and finishes when the effect has run.
            def finish_after() -> None:
                after()
                self.store.finish(
                    op.id, self.clock(), state="succeeded", result=outcome.model_dump(mode="json")
                )

            return outcome.model_copy(update={"operation_id": op.id, "after": finish_after})
        if spec.deferred:
            return outcome.model_copy(update={"operation_id": op.id})
        self.store.finish(
            op.id, self.clock(), state="succeeded", result=outcome.model_dump(mode="json")
        )
        self.after_commit(op)
        return outcome.model_copy(update={"operation_id": op.id})


def record_plan_operation[R](
    store: OperationStore,
    spec: OperationSpec,
    *,
    call: Callable[[], R],
    result: Callable[[R], dict[str, Any]],
    clock: Callable[[], float] = time.time,
    generation: str | None = None,
) -> tuple[str, R]:
    """One write to a plan or the forge as a recorded operation, for
    whoever holds the store: a route answering a person, or the daemon
    acting for an agent. Accept ``spec``, claim it for ``generation``, make
    the ``call`` and finish the operation — ``succeeded`` with ``result``
    of what came back, and the operation's id and that value are returned.

    Nothing is called and nothing new is recorded when the idempotency
    pair names an operation that exists: :class:`OperationReplay` carries
    it when the request is the same, :class:`IdempotencyConflict` when it
    differs. A :class:`~lantern.plans.PlanRefusal` from ``call`` finishes
    the operation ``failed`` with the refusal's code and detail, its
    status and extras kept as the result so a replay can answer the same
    refusal, and is re-raised naming its record (``extra["operation_id"]``).
    Any other exception finishes it ``failed`` with ``code="crashed"`` and
    is re-raised too. A process that dies in between leaves the row
    ``running`` for :func:`reconcile_operations`.
    """
    # The plan service imports this package (its principal), so the
    # refusal is looked up when one can first be raised, not at import.
    from lantern.plans.service_base import PlanRefusal

    op, created = store.accept(spec, clock())
    if not created:
        raise OperationReplay(op)
    store.claim(op.id, generation, clock())
    try:
        value = call()
    except PlanRefusal as exc:
        store.finish(
            op.id,
            clock(),
            state="failed",
            result={"status": exc.status, "extra": dict(exc.extra)},
            error_code=exc.code,
            error_detail=exc.detail,
        )
        exc.extra.setdefault("operation_id", op.id)
        raise
    except Exception as exc:
        store.finish(
            op.id,
            clock(),
            state="failed",
            error_code="crashed",
            error_detail=f"{type(exc).__name__}: {exc}"[:2000],
        )
        raise
    store.finish(op.id, clock(), state="succeeded", result=result(value))
    return op.id, value


def reconcile_operations(loop: Any, *, generation: str, now: float) -> list[Operation]:
    """Settle what a previous generation left unfinished, from evidence.

    An ``accepted`` row was never claimed: the process died before it
    could act, or the command sat past its deadline — ``expired`` either
    way, so stale intent is never applied at boot. A ``running`` row
    claimed by another generation is judged per action from what the
    domain shows: a run's state, a gate's state, an item's state. Where
    the evidence does not decide, the row is ``reconciling`` with the
    reason, for an operator; it is never guessed ``succeeded``. Returns
    the operations touched.
    """
    store: OperationStore = loop.operations
    touched: list[Operation] = []
    for op in store.pending():
        if op.state == "reconciling":
            continue
        if op.state == "accepted":
            why = (
                "past its deadline before it was claimed"
                if op.expires_at is not None and now > op.expires_at
                else "the daemon restarted before the command was claimed"
            )
            done = store.finish(op.id, now, state="expired", error_detail=why)
        elif op.claimed_generation == generation:
            continue
        else:
            state, code, detail = _judge(loop, op)
            done = store.finish(op.id, now, state=state, error_code=code, error_detail=detail)
        if done is not None:
            touched.append(done)
            log.info(
                "operation.reconciled",
                operation=op.id,
                action=op.action,
                state=done.state,
                detail=done.error_detail,
            )
    return touched


def _judge(
    loop: Any, op: Operation
) -> tuple[Literal["succeeded", "failed", "reconciling"], str | None, str | None]:
    """The verdict on a claimed operation from what the domain shows."""
    from lantern.engine.model import TERMINAL_RUN_STATES
    from lantern.errors import LanternError

    if op.action in ("run.cancel", "run.cancel_provider"):
        try:
            record = loop.store.get_run(op.target_key)
        except LanternError:
            return "failed", "unknown_target", "no such run"
        if record.state == "cancelled":
            return "succeeded", None, None
        if record.state in TERMINAL_RUN_STATES:
            return "failed", "target_already_terminal", f"run is {record.state}"
        return "failed", "interrupted_before_effect", f"run is {record.state}; cancel was lost"
    if op.action == "run.steer":
        from lantern.daemon.controls.steering import SteeringStore

        record = SteeringStore(loop.dstore).for_operation(op.id)
        if record is None:
            return "failed", "interrupted_before_effect", "no steering record was written"
        if record.status in ("delivered", "handled"):
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", f"the instruction is {record.status}"
    if op.action == "gate.approve":
        gate = loop.dstore.merge_gate_for(op.target_key)
        if gate is None:
            return "failed", "unknown_target", "no gate for the target"
        if gate.state in ("merged", "released"):
            return "succeeded", None, None
        if gate.state == "approving":
            return "reconciling", None, "the gate is still being completed"
        return "failed", "not_eligible", f"gate is {gate.state}"
    if op.action == "run.resume":
        item_id = loop.dstore.item_for_run(op.target_key)
        item = loop.dstore.get(item_id) if item_id else None
        if (
            item is not None
            and item.run_id == op.target_key
            and item.state in ("queued", "running", "done")
        ):
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the run was not admitted"
    if op.action == "item.admit" and (op.request or {}).get("form") == "plan":
        # A breakdown is queued under the id it was recorded against (a
        # person's through the routes, or the planner's through the plan
        # driver): the row being there is the effect.
        if loop.dstore.get(op.target_key) is not None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the breakdown was not queued"
    if op.action in ("item.abandon", "item.retry", "item.requeue"):
        item = loop.dstore.get(op.target_key)
        if item is None:
            return "failed", "unknown_target", "no such item"
        expected = {"item.abandon": "failed", "item.retry": "queued", "item.requeue": "queued"}
        if item.state == expected[op.action]:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", f"item is {item.state}"
    if op.action in ("item.dismiss", "item.undismiss", "run.dismiss", "run.undismiss"):
        # The mark is the effect: it stands or it does not. A run a work
        # item pins carries its mark on the item, as the verb wrote it.
        kind, key = op.target_kind, op.target_key
        if kind == "run":
            owner = loop.dstore.item_for_run(key)
            pinning = loop.dstore.get(owner) if owner else None
            if pinning is not None and pinning.run_id == key:
                kind, key = "item", pinning.item_id
        else:
            named = loop.dstore.get(key)
            if named is None:
                return "failed", "unknown_target", "no such item"
            key = named.item_id
        standing = loop.dstore.work_mark(kind, key, "dismissed") is not None
        if standing != op.action.endswith(".undismiss"):
            return "succeeded", None, None
        return (
            "failed",
            "interrupted_before_effect",
            "the alert is still dismissed" if standing else "the alert was not dismissed",
        )
    if op.action in ("item.delete", "run.delete"):
        # The mark is written last, after the directories are gone, and
        # every step before it is safe to repeat.
        kind, key = op.target_kind, op.target_key
        if kind == "item":
            named = loop.dstore.get(key)
            key = named.item_id if named is not None else key
        else:
            owner = loop.dstore.item_for_run(key)
            pinning = loop.dstore.get(owner) if owner else None
            if pinning is not None and pinning.run_id == key:
                kind, key = "item", pinning.item_id
        if loop.dstore.work_mark(kind, key, "deleted") is not None:
            return "succeeded", None, None
        return (
            "failed",
            "interrupted_before_effect",
            "the delete was interrupted; sending it again finishes it",
        )
    if op.action == "attention.dismiss_all":
        # Each alert is its own mark and dismissing twice changes nothing,
        # so what the walk left is safe to send again.
        return (
            "failed",
            "interrupted_before_effect",
            "the bulk dismissal was interrupted; sending it again dismisses what is left",
        )
    if op.action in ("daemon.stop", "daemon.restart"):
        # The process exited and a new generation is answering: that is
        # exactly the effect these promise.
        return "succeeded", None, None
    if op.action in ("daemon.pause", "daemon.release"):
        holds = set(loop.holds)
        held = op.target_key in holds
        if (op.action == "daemon.pause") == held:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the hold did not survive the restart"
    if op.action == "plan.propose":
        # The planner's draft is one transaction under the id the
        # operation named: the plan being there is the effect.
        from lantern.plans.store import PlanStore

        if PlanStore(loop.dstore).get(op.target_key) is not None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the proposed plan was not stored"
    if op.action == "plan.approve":
        # One transaction on the plan, which moves its revision: the
        # children it named say whether it landed, and a plan still at the
        # revision the approve read was never written to at all.
        from lantern.plans.store import PlanStore

        request = op.request or {}
        plan = PlanStore(loop.dstore).get(op.target_key)
        if plan is None:
            return "failed", "unknown_target", "no such plan"
        children = plan.children(str(request.get("node_id") or ""))
        if request.get("node_ids") is not None:
            named = set(request["node_ids"])
            children = [child for child in children if child.id in named]
        written = op.expected_revision is None or plan.revision > op.expected_revision
        if written and children and all(c.state not in ("draft", "proposed") for c in children):
            return "succeeded", None, None
        return (
            "failed",
            "interrupted_before_effect",
            "the approval was interrupted before it was written; approving the level again is safe",
        )
    if op.action == "plan.publish":
        # Each node is recorded as it lands and found again by its marker,
        # so what the walk left is safe to repeat under a new key.
        return (
            "failed",
            "interrupted_before_effect",
            "the publish was interrupted; publishing the level again resumes it "
            "without duplicating an issue",
        )
    if op.action == "plan.replan.approve":
        # An addition is found again by its marker, a change is guarded
        # against the issue having moved and a closed issue is not closed
        # twice, so approving what is left of the diff again is safe.
        return (
            "failed",
            "interrupted_before_effect",
            "the re-plan's approval was interrupted; approving what is left of it again "
            "resumes it without duplicating an issue",
        )
    if op.action == "plan.run":
        # The run is recorded before any task is admitted, and the daemon
        # drives a recorded run on its own: recorded is started.
        from lantern.plans.epicrun import EpicRunStore

        node_id = str((op.request or {}).get("node_id") or "")
        run = EpicRunStore(loop.dstore).latest(op.target_key, node_id)
        if run is not None and run.created_at >= op.accepted_at:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the epic run was not started"
    if op.action.startswith("plan.run."):
        # Each control is one transaction on the run (a retry's item is
        # re-queued first, and the next pass follows it): the run's
        # state, or the task's, says whether it happened.
        from lantern.plans.epicrun import EpicRunStore

        runs = EpicRunStore(loop.dstore)
        node_id = str((op.request or {}).get("node_id") or "")
        verb = op.action.removeprefix("plan.run.")
        if verb in ("pause", "resume", "cancel"):
            run = runs.latest(op.target_key, node_id)
            wanted = {"pause": ("paused",), "resume": ("running", "completed")}.get(
                verb, ("cancelled",)
            )
            if run is not None and run.state in wanted:
                return "succeeded", None, None
            done = {"pause": "paused", "resume": "resumed"}.get(verb, "cancelled")
            return "failed", "interrupted_before_effect", f"the epic run was not {done}"
        run = runs.for_task(op.target_key, node_id)
        task = run.task(node_id) if run is not None else None
        if task is not None and (
            task.state == "skipped" if verb == "skip" else task.state not in ("failed", "blocked")
        ):
            return "succeeded", None, None
        if verb == "retry" and task is not None and task.item_id is not None:
            # Re-queued before the task row was written: the next pass
            # follows the item and records the retry.
            item = loop.dstore.get(task.item_id)
            if item is not None and item.state in ("queued", "running"):
                return "succeeded", None, None
        done = "skipped" if verb == "skip" else "retried"
        return "failed", "interrupted_before_effect", f"the task was not {done}"
    if op.action.startswith("grant."):
        return _judge_grant(loop, op)
    if op.action == "decision.decline":
        # One write to the ledger row: it is resolved, or it is not.
        from lantern.daemon.controls.delegation_store import DelegationStore

        decision = DelegationStore(loop.dstore).decision(op.target_key)
        if decision is None:
            return "failed", "unknown_target", "no such decision"
        if decision.resolved_at is not None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the escalation is still waiting"
    if op.action.startswith("goal."):
        return _judge_goal(loop, op)
    if op.action == "daemon.breaker_reset":
        opened_at, _ = loop.dstore.breaker()
        if opened_at is None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the breaker is still open"
    return "reconciling", None, "the effect could not be established from the record"


def _judge_grant(
    loop: Any, op: Operation
) -> tuple[Literal["succeeded", "failed", "reconciling"], str | None, str | None]:
    """A grant write is one transaction, so the stored grant says whether
    it happened: it is there, it holds the change, or it is gone."""
    from lantern.daemon.controls.delegation_store import DelegationStore

    if op.action == "grant.restore_defaults":
        # One transaction writes every missing default: they are all in
        # place, or the restore did not happen.
        from lantern.daemon.controls.delegation_defaults import DEFAULT_GRANTS

        store = DelegationStore(loop.dstore)
        present = {grant.default_key for grant in store.grants() if grant.default_key}
        ready = getattr(loop, "_seedable_defaults", None)
        wanted = ready() if callable(ready) else list(DEFAULT_GRANTS)
        missing = [default.key for default in wanted if default.key not in present]
        if not missing:
            return "succeeded", None, None
        return (
            "failed",
            "interrupted_before_effect",
            f"the default grants were not restored ({', '.join(missing)} missing)",
        )
    grant = DelegationStore(loop.dstore).grant(op.target_key)
    if op.action == "grant.create":
        if grant is not None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the grant was not written"
    if op.action == "grant.delete":
        if grant is None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the grant is still there"
    if op.action == "grant.update":
        if grant is None:
            return "failed", "unknown_target", "no such grant"
        changes = dict((op.request or {}).get("changes") or {})
        held = {
            "conditions": grant.conditions.as_dict(),
            "daily_limit": grant.daily_limit,
            "enabled": grant.enabled,
            "note": grant.note,
        }
        moved = op.expected_revision is None or grant.revision > op.expected_revision
        if changes and moved and all(held.get(key) == value for key, value in changes.items()):
            return "succeeded", None, None
        return (
            "failed",
            "interrupted_before_effect",
            f"the grant does not hold the requested change (it is at revision {grant.revision}); "
            "read it and send the change again if it is still wanted",
        )
    return "reconciling", None, "the effect could not be established from the record"


def _judge_goal(
    loop: Any, op: Operation
) -> tuple[Literal["succeeded", "failed", "reconciling"], str | None, str | None]:
    """A goal write is one transaction, so the stored goal says whether it
    happened: it is there, it holds the change, or it is gone."""
    from lantern.daemon.goals import GoalStore

    goal = GoalStore(loop.dstore).goal(op.target_key)
    if op.action == "goal.create":
        if goal is not None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the goal was not written"
    if op.action == "goal.delete":
        if goal is None:
            return "succeeded", None, None
        return "failed", "interrupted_before_effect", "the goal is still there"
    if op.action == "goal.update":
        if goal is None:
            return "failed", "unknown_target", "no such goal"
        changes = dict((op.request or {}).get("changes") or {})
        held = {"title": goal.title, "text": goal.text, "state": goal.state}
        moved = op.expected_revision is None or goal.revision > op.expected_revision
        if changes and moved and all(held.get(key) == value for key, value in changes.items()):
            return "succeeded", None, None
        return (
            "failed",
            "interrupted_before_effect",
            f"the goal does not hold the requested change (it is at revision {goal.revision}); "
            "read it and send the change again if it is still wanted",
        )
    return "reconciling", None, "the effect could not be established from the record"
