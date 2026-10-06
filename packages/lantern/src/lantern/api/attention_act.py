"""Acting on what is waiting, by the entry (``POST /v1/attention/{id}/act``).

A client that reads ``GET /v1/attention`` holds an entry and the names of
the actions it offers. To take one it used to need a dozen routes, each
with its own ids, revision and idempotency rules — more than a
notification's action button can carry. Here the entry's id and the
action's name are enough.

Nothing in this module acts. It finds the entry as it stands now, checks
that the action is one the entry offers and the caller may take, and
hands the request to the function the action's own route calls
(:mod:`lantern.api.commands`, :mod:`lantern.api.admin`, the epic run's
control). The operation recorded is that command's, under the caller's
idempotency key scoped to this route and this entry; nothing is recorded
for the act itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field, ValidationError

from lantern.api import escalations
from lantern.api.admin import resume_repository
from lantern.api.attention import (
    DECISION_KINDS,
    PROPOSAL_CAPABILITY,
    REPOSITORY_ACTIONS,
    TASK_ACTIONS,
    WORK_COMMANDS,
    capability_for,
    find,
)
from lantern.api.auth.deps import Authenticated
from lantern.api.commands import admit_plan, approve_gate, item_command, run_verb, steer
from lantern.api.context import ApiContext
from lantern.api.delegation import decision_out
from lantern.api.errors import Problem
from lantern.api.models import (
    ApiModel,
    AttentionEntry,
    DecisionOut,
    GateApproval,
    GateResult,
    ItemCommand,
    ItemCommandResult,
    RepositoryResult,
    RoundGrant,
    RunCommand,
    RunCommandResult,
    SteerRequest,
    SteerResult,
    WorkDeleteCommand,
)
from lantern.api.plan_schemas import (
    EpicRunChanged,
    EpicRunStart,
    EpicRunStarted,
    PlanApprove,
    PlanBreakdownAccepted,
    PlanOut,
    PlanPublish,
    PlanPublished,
)
from lantern.api.projections import Views, not_found
from lantern.api.publicids import run_public_id
from lantern.api.routes.plan_runs import ControlVerb, control_epic_run, start_epic_run
from lantern.api.routes.plans import (
    actor_of,
    approve_level,
    plan_out,
    problem_of,
    publish_level_as,
)
from lantern.daemon.controls.delegation_store import DecisionRecord, DelegationStore
from lantern.daemon.controls.intake import PlanAdmission
from lantern.daemon.controls.operations import (
    IdempotencyConflict,
    Operation,
    OperationReplay,
    OperationSpec,
    OperationStore,
    record_plan_operation,
)
from lantern.plans import PlanRefusal
from lantern.plans.store import actor_id


class AttentionActRequest(ApiModel):
    """One action on one entry. ``action`` is a name the entry lists in
    ``actions``; ``expected_revision`` is the entry's ``revision`` as the
    person read it; ``params`` are the action's own arguments, the fields
    its own route takes in its body."""

    action: str = Field(min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)


class PlanApproved(ApiModel):
    """A level approved from the list: the plan as it now is and the
    ``plan.approve`` operation that approved it."""

    plan: PlanOut
    operation_id: str
    replayed: bool = False


class EscalationDeclined(ApiModel):
    """An escalation a person said no to: the decision as the ledger now
    holds it, and the ``decision.decline`` operation that recorded it."""

    decision: DecisionOut
    operation_id: str
    replayed: bool = False


#: What an act answers with: the body the action's own route answers.
ActOutcome = (
    ItemCommandResult
    | RunCommandResult
    | SteerResult
    | GateResult
    | RepositoryResult
    | EpicRunChanged
    | EpicRunStarted
    | PlanPublished
    | PlanBreakdownAccepted
    | PlanApproved
    | EscalationDeclined
)


class AttentionActResult(ApiModel):
    """What became of an act: the command's own answer under ``result``,
    the operation it recorded, and whether the entry still waits."""

    entry_id: str
    action: str
    #: The operation the action's command recorded: `GET /v1/operations/{id}`.
    operation_id: str | None = None
    #: True when this answers an earlier act under the same
    #: ``Idempotency-Key``: nothing was done again.
    replayed: bool = False
    #: Whether an entry under this id is on the list (dismissed alerts
    #: left out) as this answer is written.
    still_waiting: bool
    result: ActOutcome
    #: On an ``escalation``: the decision as the ledger holds it after the
    #: act — ``acted`` by the caller on an approve, ``declined`` on a
    #: decline (the first resolution stands).
    decision: DecisionOut | None = None


@dataclass(frozen=True, slots=True)
class _Route:
    """Where an action goes: the operation its command records, whose
    revision that command checks (``None``: it takes none), and the body
    model its own route validates (``None``: it takes no body)."""

    operation: str
    checks: str | None = None
    body: type[ApiModel] | None = None


_BODIES: dict[str, tuple[str | None, type[ApiModel] | None]] = {
    "gate_approve": ("gate", GateApproval),
    "review_wait_resume": (None, None),
    "grant_rounds": ("run", RoundGrant),
    "resume": ("run", RunCommand),
    "retry": ("item", ItemCommand),
    "requeue": ("item", ItemCommand),
    "steer": ("run", SteerRequest),
    "cancel": ("run", RunCommand),
    "abandon": ("item", ItemCommand),
    "dismiss": ("item", ItemCommand),
    "undismiss": ("item", ItemCommand),
    "delete": ("item", WorkDeleteCommand),
}

#: The operation each action records: what a replay under the same key is
#: told apart from another action by.
OPERATIONS: dict[str, str] = {
    **WORK_COMMANDS,
    "task_retry": "plan.run.retry",
    "task_skip": "plan.run.skip",
    "repository_resume": "repo.resume",
}

_ROUTES: dict[str, _Route] = {
    action: _Route(operation, *_BODIES.get(action, (None, None)))
    for action, operation in OPERATIONS.items()
}

#: What an ``escalation`` or a ``plan_proposal`` offers: what each does
#: depends on the entry, not on the name (:func:`_decide`).
DECISION_ACTIONS: tuple[str, ...] = (escalations.APPROVE, escalations.DECLINE)
KNOWN_ACTIONS: list[str] = sorted({*_ROUTES, *DECISION_ACTIONS})

#: The operation approving each escalated action records: the one the
#: step's own route records, under the person.
APPROVED_OPERATIONS: dict[str, str] = {
    "plan.breakdown": "item.admit",
    "plan.approve": "plan.approve",
    "plan.publish": "plan.publish",
    "plan.run": "plan.run",
    "plan.run.retry": "plan.run.retry",
    "item.retry": "item.retry",
    "run.grant_rounds": "run.grant_rounds",
}


@dataclass(frozen=True, slots=True)
class _Targets:
    """The public ids an action's command is named by."""

    item_id: str | None = None
    run_id: str | None = None
    gate_id: str | None = None
    repository_id: str | None = None
    plan_id: str | None = None
    node_id: str | None = None

    @classmethod
    def of(cls, entry: AttentionEntry) -> _Targets:
        return cls(
            item_id=entry.item_id,
            run_id=entry.run_id,
            gate_id=entry.gate_id,
            repository_id=entry.repository_id,
            plan_id=entry.plan_id,
            node_id=entry.node_id,
        )

    @classmethod
    def replayed(cls, views: Views, action: str, op: Operation) -> _Targets:
        """What an earlier act under the same key was aimed at, read from
        the operation it recorded: the entry itself may be gone."""
        if action in TASK_ACTIONS:
            return cls(
                plan_id=str(op.request.get("plan_id") or ""),
                node_id=str(op.request.get("node_id") or ""),
            )
        if action in REPOSITORY_ACTIONS:
            return cls(repository_id=views.ids.repository_id(op.target_key, views.now))
        if action == "gate_approve":
            return cls(gate_id=views.ids.gate_id(op.target_key, views.now))
        if op.target_kind == "item":
            item = views.dstore.get(op.target_key)
            if item is None:
                raise not_found()
            return cls(item_id=views.ids.item_id(item, views.now))
        return cls(run_id=run_public_id(op.target_key))


def _invalid(detail: str, loc: list[str], msg: str) -> Problem:
    return Problem(422, "invalid_request", detail, errors=[{"loc": loc, "msg": msg}])


def _command(route: _Route, body: AttentionActRequest, *, pinned: bool) -> ApiModel | None:
    """The body the action's own route would have been sent, validated by
    that route's model. ``pinned``: the command checks the revision of the
    record the entry's own revision is of, and is handed it."""
    action, params = body.action, body.params
    if "expected_revision" in params:
        raise _invalid(
            "expected_revision is a field of the request, not one of params",
            ["params", "expected_revision"],
            "send it beside action",
        )
    if route.body is None:
        if params:
            raise _invalid(
                f"{action} takes no params",
                ["params", sorted(params)[0]],
                "this action takes no params",
            )
        return None
    fields = dict(params)
    if pinned and body.expected_revision is not None:
        fields["expected_revision"] = body.expected_revision
    try:
        return route.body.model_validate(fields)
    except ValidationError as exc:
        raise Problem(
            422,
            "invalid_request",
            f"the request is not a valid {action}",
            errors=[
                {
                    "loc": (
                        ["expected_revision"]
                        if tuple(e["loc"]) == ("expected_revision",)
                        else ["params", *e["loc"]]
                    ),
                    "msg": e["msg"],
                }
                for e in exc.errors()
            ],
        ) from exc


def _need(value: str | None, action: str, entry_id: str) -> str:
    if not value:
        raise Problem(
            409,
            "not_eligible",
            f"{entry_id} names nothing {action} can be taken on",
            action=action,
            entry_id=entry_id,
        )
    return value


async def _dispatch(
    ctx: ApiContext,
    auth: Authenticated,
    entry_id: str,
    action: str,
    targets: _Targets,
    command: ApiModel | None,
    pair: tuple[str, str],
) -> tuple[ActOutcome, int]:
    """Run the action's command; its answer and the status its own route
    answers with."""
    family, _, verb = _ROUTES[action].operation.partition(".")
    if isinstance(command, GateApproval):
        gate_id = _need(targets.gate_id, action, entry_id)
        return await approve_gate(ctx, auth, gate_id, command, pair), 202
    if isinstance(command, SteerRequest):
        run_id = _need(targets.run_id, action, entry_id)
        return await steer(ctx, auth, run_id, command, pair), 202
    if family == "item":
        assert command is None or isinstance(command, ItemCommand | WorkDeleteCommand)  # nosec B101
        item_id = _need(targets.item_id, action, entry_id)
        return await item_command(ctx, auth, verb, item_id, command, pair), 200  # type: ignore[arg-type]
    if family == "run":
        assert command is None or isinstance(command, RunCommand | RoundGrant)  # nosec B101
        run_id = _need(targets.run_id, action, entry_id)
        changed = await run_verb(ctx, auth, verb, run_id, command, pair)  # type: ignore[arg-type]
        # A cancel honoured at the run's next boundary is accepted, not done.
        accepted = verb == "cancel" and changed.operation.state == "running"
        return changed, 202 if accepted else 200
    if family == "plan":
        plan_id = _need(targets.plan_id, action, entry_id)
        node_id = _need(targets.node_id, action, entry_id)
        control: ControlVerb = "retry" if action == "task_retry" else "skip"
        return await control_epic_run(ctx, auth, control, plan_id, node_id, pair), 200
    repository_id = _need(targets.repository_id, action, entry_id)
    return await resume_repository(ctx, auth, repository_id, pair), 200


async def act(
    ctx: ApiContext,
    auth: Authenticated,
    entry_id: str,
    body: AttentionActRequest,
    pair: tuple[str, str],
) -> tuple[AttentionActResult, int]:
    """Take ``body.action`` on the entry ``entry_id`` names; the answer and
    its HTTP status."""
    action = body.action
    kind = entry_id.partition(":")[0]
    if kind in DECISION_KINDS:
        return await _decide(ctx, auth, entry_id, body, pair)
    route = _ROUTES.get(action)
    if route is None:
        if action in DECISION_ACTIONS:
            # A name some entries offer, and this one never does.
            raise Problem(
                409,
                "not_eligible",
                f"{entry_id} does not offer {action}; only an escalation or a proposal does",
                action=action,
                entry_id=entry_id,
            )
        raise Problem(422, "unknown_action", f"no such action {action!r}", actions=KNOWN_ACTIONS)
    capability = capability_for(action)
    if not auth.principal.can(capability):  # type: ignore[arg-type]
        raise Problem(
            403, "forbidden", f"{auth.principal.id} lacks {capability}", capability=capability
        )
    # An entry's revision is its gate's or its item's. A command that
    # checks that same record is handed the revision and refuses a moved
    # one inside its own operation; for any other the entry is compared
    # here. Approving is only ever a gate's.
    pinned = route.checks is not None and route.checks in (kind, "gate")
    command = _command(route, body, pinned=pinned)

    def look() -> tuple[_Targets, bool]:
        store = getattr(ctx.loop, "operations", None)
        if not isinstance(store, OperationStore):
            raise Problem(503, "daemon_not_ready", "the daemon keeps no operation record")
        views = Views(ctx)
        existing = store.for_idempotency(*pair)
        if existing is not None:
            # The key already names an act on this entry. Its command
            # answers the replay — or the conflict, when the arguments
            # differ; another action under the key is one here.
            if existing.action != route.operation:
                raise Problem(
                    409,
                    "idempotency_conflict",
                    "the idempotency key was already used with a different request",
                    operation_id=existing.id,
                )
            return _Targets.replayed(views, action, existing), True
        entry = find(views, entry_id, auth, include_dismissed=True)
        if entry is None:
            raise Problem(
                409,
                "not_waiting",
                f"nothing is waiting as {entry_id}: it was settled, or it waits again "
                "as a new entry",
                entry_id=entry_id,
            )
        offered = [offer.action for offer in entry.actions]
        if action not in offered:
            raise Problem(
                409,
                "not_eligible",
                f"{entry_id} does not offer {action} now; it offers: "
                f"{', '.join(offered) or 'nothing'}",
                action=action,
                offered=offered,
                entry_id=entry_id,
            )
        if not pinned and body.expected_revision is not None:
            if entry.revision is None:
                raise _invalid(
                    f"{entry_id} carries no revision to act on",
                    ["expected_revision"],
                    "this entry has no revision",
                )
            if entry.revision != body.expected_revision:
                raise Problem(
                    409,
                    "stale_revision",
                    f"{entry_id} is at revision {entry.revision}, not {body.expected_revision}",
                    revision=entry.revision,
                    entry_id=entry_id,
                )
        return _Targets.of(entry), False

    targets, replayed = await ctx.call(look)
    result, status = await _dispatch(ctx, auth, entry_id, action, targets, command, pair)
    still_waiting = await ctx.call(lambda: find(Views(ctx), entry_id, auth) is not None)
    operation = getattr(result, "operation", None)
    return (
        AttentionActResult(
            entry_id=entry_id,
            action=action,
            operation_id=operation.id if operation is not None else result.operation_id,  # type: ignore[union-attr]
            replayed=replayed,
            still_waiting=still_waiting,
            result=result,
        ),
        status,
    )


# -- escalations and proposals ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Decided:
    """What an act on an ``escalation`` or a ``plan_proposal`` is aimed
    at, read in one pass: the ledger row (none for a proposal), the public
    ids its command is named by, the plan revision it is sent with, and
    the operation an earlier act under the same key recorded."""

    record: DecisionRecord | None
    operation: str
    plan_id: str | None = None
    node_id: str | None = None
    item_id: str | None = None
    run_id: str | None = None
    revision: int | None = None
    replay: Operation | None = None


def _forbidden(auth: Authenticated, capability: str) -> Problem:
    return Problem(
        403, "forbidden", f"{auth.principal.id} lacks {capability}", capability=capability
    )


def _params(action: str, record: DecisionRecord | None, params: dict[str, Any]) -> None:
    """Only approving a round grant takes a param (``rounds``)."""
    if "expected_revision" in params:
        raise _invalid(
            "expected_revision is a field of the request, not one of params",
            ["params", "expected_revision"],
            "send it beside action",
        )
    grants = (
        action == escalations.APPROVE and record is not None and record.action == "run.grant_rounds"
    )
    extra = sorted(set(params) - ({"rounds"} if grants else set()))
    if extra:
        raise _invalid(
            f"{action} takes no {extra[0]} here",
            ["params", extra[0]],
            "this action takes no such param",
        )


async def _decide(
    ctx: ApiContext,
    auth: Authenticated,
    entry_id: str,
    body: AttentionActRequest,
    pair: tuple[str, str],
) -> tuple[AttentionActResult, int]:
    """``approve`` or ``decline`` an escalation, or ``approve`` a proposed
    level. The capability is the escalated act's (a proposal's is
    ``plans:create``). ``approve`` runs the step's own command as the
    caller — its operation, its refusals — then resolves the decision
    ``acted`` by them; ``decline`` records a ``decision.decline``
    operation that resolves it ``declined``. A replay under the same key
    answers the first act."""
    action = body.action
    kind, _, key = entry_id.partition(":")
    offers = DECISION_ACTIONS if kind == escalations.KIND else (escalations.APPROVE,)
    if action not in offers:
        if action in KNOWN_ACTIONS:
            raise Problem(
                409,
                "not_eligible",
                f"{entry_id} does not offer {action}; it offers: {', '.join(offers)}",
                action=action,
                offered=list(offers),
                entry_id=entry_id,
            )
        raise Problem(422, "unknown_action", f"no such action {action!r}", actions=KNOWN_ACTIONS)

    def look() -> _Decided:
        store = getattr(ctx.loop, "operations", None)
        if not isinstance(store, OperationStore):
            raise Problem(503, "daemon_not_ready", "the daemon keeps no operation record")
        views = Views(ctx)
        record: DecisionRecord | None = None
        if kind == escalations.KIND:
            record = DelegationStore(views.dstore).decision(key)
            if record is None or record.outcome != "escalate":
                raise Problem(
                    409, "not_waiting", f"nothing is waiting as {entry_id}", entry_id=entry_id
                )
            capability: str = escalations.capability(record.action)
            operation = (
                escalations.DECLINE_OPERATION
                if action == escalations.DECLINE
                else APPROVED_OPERATIONS.get(record.action, "")
            )
            plan_id, node_id = record.plan_id or "", record.node_id or ""
        else:
            capability = PROPOSAL_CAPABILITY
            operation = "plan.approve"
            plan_id, _, node_id = key.partition(":")
        if not auth.principal.can(capability):  # type: ignore[arg-type]
            raise _forbidden(auth, capability)
        _params(action, record, body.params)
        existing = store.for_idempotency(*pair)
        if existing is not None:
            if existing.action != operation:
                raise Problem(
                    409,
                    "idempotency_conflict",
                    "the idempotency key was already used with a different request",
                    operation_id=existing.id,
                )
            raw = (existing.request or {}).get("expected_revision")
            revision = raw if isinstance(raw, int) else None
        else:
            entry = find(views, entry_id, auth)
            if entry is None:
                raise Problem(
                    409,
                    "not_waiting",
                    f"nothing is waiting as {entry_id}: it was settled, or what it is "
                    "about is gone",
                    entry_id=entry_id,
                )
            offered = [offer.action for offer in entry.actions]
            if action not in offered:
                raise Problem(
                    409,
                    "not_eligible",
                    f"{entry_id} does not offer {action} now; it offers: "
                    f"{', '.join(offered) or 'nothing'}",
                    action=action,
                    offered=offered,
                    entry_id=entry_id,
                )
            if body.expected_revision is not None and entry.revision != body.expected_revision:
                raise Problem(
                    409,
                    "stale_revision",
                    f"{entry_id} is at revision {entry.revision}, not {body.expected_revision}",
                    revision=entry.revision,
                    entry_id=entry_id,
                )
            revision = entry.revision
        item_public = run_public = None
        if record is not None and (record.item_id or record.run_id):
            item = views.dstore.get(record.item_id) if record.item_id else None
            if item is not None:
                item_public = views.ids.item_id(item, views.now)
            run = record.run_id or (item.run_id if item is not None else None)
            run_public = run_public_id(run) if run else None
        return _Decided(
            record=record,
            operation=operation,
            plan_id=plan_id or None,
            node_id=node_id or None,
            item_id=item_public,
            run_id=run_public,
            revision=revision,
            replay=existing,
        )

    target = await ctx.call(look)
    decision: DecisionOut | None = None
    result: ActOutcome
    if action == escalations.DECLINE:
        assert target.record is not None  # nosec B101 - only an escalation declines
        declined = await _decline(ctx, auth, target.record, pair)
        result, status, decision = declined, 200, declined.decision
    else:
        result, status = await _approve(ctx, auth, entry_id, target, body.params, pair)
        if target.record is not None:
            decision = await ctx.call(_resolve_acted, ctx, auth, target.record)
    still_waiting = await ctx.call(lambda: find(Views(ctx), entry_id, auth) is not None)
    operation = getattr(result, "operation", None)
    return (
        AttentionActResult(
            entry_id=entry_id,
            action=action,
            operation_id=operation.id if operation is not None else result.operation_id,  # type: ignore[union-attr]
            replayed=target.replay is not None,
            still_waiting=still_waiting,
            result=result,
            decision=decision,
        ),
        status,
    )


def _resolve_acted(ctx: ApiContext, auth: Authenticated, record: DecisionRecord) -> DecisionOut:
    """The step happened, taken by the caller: the decision is ``acted``
    by them (the first resolution stands)."""
    done = DelegationStore(ctx.loop.dstore).resolve(
        record.id, by=actor_id(actor_of(auth)), resolution="acted", now=ctx.clock()
    )
    return decision_out(ctx, done or record)


async def _decline(
    ctx: ApiContext, auth: Authenticated, record: DecisionRecord, pair: tuple[str, str]
) -> EscalationDeclined:
    """Resolve the escalation ``declined`` by the caller, as one recorded
    ``decision.decline`` operation: a replay answers the decision as the
    ledger holds it, and changes nothing."""
    delegation = DelegationStore(ctx.loop.dstore)
    by = actor_id(actor_of(auth))

    def apply() -> EscalationDeclined:
        try:
            op_id, _ = record_plan_operation(
                ctx.loop.operations,
                OperationSpec(
                    action=escalations.DECLINE_OPERATION,
                    target_kind="decision",
                    target_key=record.id,
                    principal=auth.principal,
                    request={"decision_id": record.id},
                    idempotency=pair,
                ),
                call=lambda: delegation.resolve(
                    record.id, by=by, resolution="declined", now=ctx.clock()
                ),
                result=lambda done: {"resolution": done.resolution if done else None},
                clock=ctx.clock,
                generation=getattr(ctx.loop, "generation", None),
            )
            replayed = False
        except OperationReplay as exc:
            op_id, replayed = exc.existing.id, True
        except IdempotencyConflict as exc:
            raise Problem(
                409,
                "idempotency_conflict",
                "the idempotency key was already used with a different request",
                operation_id=exc.existing.id,
            ) from exc
        held = delegation.decision(record.id) or record
        return EscalationDeclined(
            decision=decision_out(ctx, held), operation_id=op_id, replayed=replayed
        )

    declined = await ctx.call(apply)
    ctx.hub.notify()
    return declined


def _revision(target: _Decided, entry_id: str) -> int:
    if target.revision is None:
        raise Problem(
            409, "not_eligible", f"{entry_id} names no plan revision to act on", entry_id=entry_id
        )
    return target.revision


async def _approve(
    ctx: ApiContext,
    auth: Authenticated,
    entry_id: str,
    target: _Decided,
    params: dict[str, Any],
    pair: tuple[str, str],
) -> tuple[ActOutcome, int]:
    """Take the step the agent proposed (or approve the proposed level) as
    the caller, through the command the step's own route runs; its answer
    and the status that route answers with."""
    record = target.record
    step = record.action if record is not None else "plan.approve"
    replayed = target.replay is not None
    approve = escalations.APPROVE
    if step in ("plan.approve", "plan.publish", "plan.run", "plan.run.retry", "plan.breakdown"):
        plan_id = _need(target.plan_id, approve, entry_id)
        node_id = _need(target.node_id, approve, entry_id)
        if step == "plan.approve":
            level = PlanApprove(expected_revision=_revision(target, entry_id))
            plan, op_id = await approve_level(ctx, auth, plan_id, node_id, level, pair)
            return PlanApproved(plan=plan_out(plan), operation_id=op_id, replayed=replayed), 200
        if step == "plan.publish":
            publish = PlanPublish(expected_revision=_revision(target, entry_id))
            return await publish_level_as(ctx, auth, plan_id, node_id, publish, pair), 200
        if step == "plan.run":
            start = EpicRunStart(expected_revision=_revision(target, entry_id))
            started = await start_epic_run(ctx, auth, plan_id, node_id, start, pair)
            return started, 200 if started.replayed else 201
        if step == "plan.run.retry":
            return await control_epic_run(ctx, auth, "retry", plan_id, node_id, pair), 200
        revision = _revision(target, entry_id)
        if not replayed:

            def check() -> None:
                ctx.plans.breakdown_target(plan_id, node_id, expected_revision=revision)

            try:
                await ctx.call(check)
            except PlanRefusal as exc:
                raise problem_of(exc) from exc
        admitted = await admit_plan(
            ctx,
            auth,
            PlanAdmission(plan_id=plan_id, node_id=node_id, expected_revision=revision),
            pair,
        )
        accepted = PlanBreakdownAccepted(
            plan_id=plan_id,
            node_id=node_id,
            item=admitted.item,
            operation=admitted.operation,
            created=admitted.created,
        )
        return accepted, 202
    if step == "item.retry":
        item_id = _need(target.item_id, approve, entry_id)
        return await item_command(ctx, auth, "retry", item_id, ItemCommand(), pair), 200
    if step == "run.grant_rounds":
        assert record is not None  # nosec B101 - an escalation's step
        run_id = _need(target.run_id, approve, entry_id)
        rounds = params.get("rounds", record.attrs.get("rounds"))
        try:
            grant = RoundGrant.model_validate({"rounds": rounds})
        except ValidationError as exc:
            raise _invalid(
                "a round grant needs how many rounds",
                ["params", "rounds"],
                "send rounds, a whole number from 1 to 100",
            ) from exc
        return await run_verb(ctx, auth, "grant_rounds", run_id, grant, pair), 200
    raise Problem(
        409,
        "not_eligible",
        f"{entry_id} asks for {step}, which no person can take from here",
        action=approve,
        entry_id=entry_id,
    )
