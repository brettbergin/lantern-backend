"""Running a recorded command from a route: the idempotency pair, the
replay and the conflict, as the spike's command contract has them.

Every mutating route builds an idempotency scope from the workspace, the
principal, the method and the canonical route, takes the client's
``Idempotency-Key``, and calls the service inside :func:`run_command`. A
replay of the same key and payload answers with the operation that
already exists (or the refusal it recorded); a different payload under
the same key is ``409 idempotency_conflict``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import Request

from lantern.api.auth.deps import Authenticated
from lantern.api.context import ApiContext
from lantern.api.errors import CONTROL_STATUS, Problem
from lantern.api.models import (
    Admitted,
    AttentionDismissed,
    AttentionDismissRequest,
    AttentionDismissResult,
    GateApproval,
    GateResult,
    IntakeRequest,
    IssueIntake,
    Item,
    ItemCommand,
    ItemCommandResult,
    OperationOut,
    RoundGrant,
    RunCommand,
    RunCommandResult,
    SteerRequest,
    SteerResult,
    ToolIntake,
    WorkDeleteCommand,
    WorkloadIntake,
)
from lantern.api.projections import NOT_FOUND, Views, not_found
from lantern.daemon.controls.eligibility import Subject, check as check_eligibility
from lantern.daemon.controls.intake import (
    AdmitRequest,
    IssueAdmission,
    PlanAdmission,
    ToolAdmission,
    WorkloadAdmission,
)
from lantern.daemon.controls.operations import (
    IdempotencyConflict,
    Operation,
    OperationReplay,
    OperationStore,
)
from lantern.daemon.controls.principal import Capability as PrincipalCapability, Principal
from lantern.daemon.controls.results import (
    AdmitOutcome,
    DeleteOutcome,
    DismissAllOutcome,
    DismissOutcome,
    ItemOutcome,
    Outcome,
)
from lantern.daemon.controls.steering import SteeringStore
from lantern.db.collaboration_models import ChannelRow
from lantern.vcs.protocol import Capability

KEY_HEADER = "Idempotency-Key"
KEY_MAX = 200


def idempotency_pair(
    principal: Principal, key: str | None, route: str, *, required: bool, method: str = "POST"
) -> tuple[str, str] | None:
    """The ``(scope, key)`` pair for a command, ``None`` when the client
    sent no key and the command does not insist on one. The scope is the
    workspace, the principal, the method and the canonical route, so one
    client's key never collides with another's."""
    key = (key or "").strip()
    if not key:
        if required:
            raise Problem(
                422,
                "idempotency_key_required",
                f"a {KEY_HEADER} header is required to admit work",
            )
        return None
    if len(key) > KEY_MAX:
        raise Problem(422, "invalid_request", f"{KEY_HEADER} is limited to {KEY_MAX} characters")
    return f"{principal.workspace_id}:{principal.id}:{method}:{route}", key


def idempotency(
    request: Request, principal: Principal, route: str, *, required: bool
) -> tuple[str, str] | None:
    """The pair for an HTTP request, from its ``Idempotency-Key`` header."""
    return idempotency_pair(
        principal, request.headers.get(KEY_HEADER), route, required=required, method=request.method
    )


class Replayed(Exception):
    """The command was a replay: the existing operation, for the route to
    answer from."""

    def __init__(self, operation: Operation) -> None:
        super().__init__(operation.id)
        self.operation = operation


async def run_command[T: Outcome](ctx: ApiContext, fn: Callable[[], T]) -> T:
    """Run ``fn`` on the executor, turning the operation store's replay
    and conflict into what the route answers."""
    try:
        return await ctx.call(fn)
    except OperationReplay as exc:
        raise Replayed(exc.existing) from exc
    except IdempotencyConflict as exc:
        raise Problem(
            409,
            "idempotency_conflict",
            "the idempotency key was already used with a different request",
            operation_id=exc.existing.id,
        ) from exc


def replayed_problem(operation: Operation) -> Problem | None:
    """What a replayed operation that did not succeed answers: the refusal
    it recorded, or a conflict while the first attempt is still running."""
    if operation.state == "succeeded":
        return None
    if operation.state in ("failed", "expired"):
        code = operation.error_code or "failed"
        return Problem(
            CONTROL_STATUS.get(code, 409),
            code,
            operation.error_detail or "the earlier attempt failed",
            operation_id=operation.id,
        )
    return Problem(
        409,
        "already_in_progress",
        "the same request is still being applied",
        operation_id=operation.id,
    )


# -- the commands themselves: one function per verb, for every transport ---------


def _operation(ctx: ApiContext, op_id: str | None) -> Operation:
    store = getattr(ctx.loop, "operations", None)
    op: Operation | None = store.get(op_id) if store is not None and op_id else None
    if op is None:
        raise Problem(503, "daemon_not_ready", "the daemon keeps no operation record")
    return op


def _check_channel(ctx: ApiContext, auth: Authenticated, channel_id: str | None) -> None:
    """A channel work is admitted for must exist and be one the caller may
    read: a workspace member names their own channels; a plain API client,
    which reads everything, any active one. Work admitted for a channel is
    delivered and charged there, so naming one takes the capabilities a
    channel write does, checked before whether the channel exists."""
    if channel_id is None:
        return
    member = auth.member
    needed: tuple[PrincipalCapability, ...] = ("collaboration:write",)
    if member is not None:
        needed = ("collaboration:read", *needed)
    for capability in needed:
        if not auth.principal.can(capability):
            raise Problem(
                403,
                "forbidden",
                f"{auth.principal.id} lacks {capability}",
                capability=capability,
            )
    if member is not None:
        found = ctx.collaboration.get_channel(member.user.id, channel_id) is not None
    else:
        with ctx.loop.dstore.read() as session:
            row = session.get(ChannelRow, channel_id)
            found = row is not None and row.state == "active"
    if not found:
        raise Problem(404, "channel_not_found", "channel not found")


def _admission(
    ctx: ApiContext, auth: Authenticated, body: IssueIntake | WorkloadIntake | ToolIntake
) -> AdmitRequest:
    """The service's request for a body; a public repository id is
    resolved here, on the executor."""
    if not isinstance(body, ToolIntake):
        _check_channel(ctx, auth, body.channel_id)
    if isinstance(body, IssueIntake):
        if (body.repository_id is None) == (body.repository is None):
            raise Problem(
                422, "invalid_request", "name the repository by `repository_id` or `repository`"
            )
        repo = body.repository
        if body.repository_id is not None:
            repo = Views(ctx).repository_by_public_id(body.repository_id).repo
        assert repo is not None  # nosec B101 - one of the two was given
        run_kind: Literal["code", "workload"] = body.run_kind
        return IssueAdmission(
            repository=repo,
            number=body.number,
            run_kind=run_kind,
            lead=body.lead,
            roles=dict(body.roles),
            channel_id=body.channel_id,
        )
    if isinstance(body, WorkloadIntake):
        return WorkloadAdmission(
            ask=body.ask,
            profile=body.profile,
            sink=body.sink,
            lead=body.lead,
            roles=dict(body.roles),
            channel_id=body.channel_id,
        )
    return ToolAdmission(recipe=body.recipe, parameters=dict(body.parameters))


async def admit(
    ctx: ApiContext, auth: Authenticated, body: IntakeRequest, pair: tuple[str, str] | None
) -> Admitted:
    """Admit work; the same function behind ``POST /v1/items`` and the
    WebSocket's ``item.admit``."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> AdmitOutcome:
        return service.admit(principal, _admission(ctx, auth, body), idempotency=pair)

    return await _admitted(ctx, apply)


async def admit_plan(
    ctx: ApiContext,
    auth: Authenticated,
    request: PlanAdmission,
    pair: tuple[str, str] | None,
) -> Admitted:
    """Queue a breakdown of a plan node as a ``plan`` run; the command
    behind ``POST /v1/plans/{id}/nodes/{node_id}/breakdown``. The channel
    it names is checked as any admission's is; the node's rules are the
    plan service's, applied inside the recorded operation."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> AdmitOutcome:
        _check_channel(ctx, auth, request.channel_id)
        return service.admit(principal, request, idempotency=pair)

    return await _admitted(ctx, apply)


async def _admitted(ctx: ApiContext, apply: Callable[[], AdmitOutcome]) -> Admitted:
    """Run an admission and answer with the item and its operation — the
    existing ones on a replay."""
    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None:
            raise problem from replay
        result = existing.result or {}
        item_id = str((result.get("item") or {}).get("item_id") or "")

        def reread() -> Item:
            views = Views(ctx)
            item = views.dstore.get(item_id) if item_id else None
            if item is None:
                raise not_found()
            return views.item(item)

        view = await ctx.call(reread)
        return Admitted(item=view, operation=OperationOut.from_operation(existing), created=False)

    def project() -> tuple[Item, Operation]:
        return Views(ctx).item(outcome.item), _operation(ctx, outcome.operation_id)

    view, op = await ctx.call(project)
    ctx.hub.notify()
    return Admitted(item=view, operation=OperationOut.from_operation(op), created=outcome.fresh)


ItemVerb = Literal["retry", "requeue", "abandon", "dismiss", "undismiss", "delete"]


async def item_command(
    ctx: ApiContext,
    auth: Authenticated,
    verb: ItemVerb,
    public_id: str,
    body: ItemCommand | WorkDeleteCommand | None,
    pair: tuple[str, str] | None,
) -> ItemCommandResult:
    """Retry, requeue or abandon an item, or dismiss its alert (and take
    that back), through the shared service."""
    principal = auth.principal
    command = body or ItemCommand()
    service = ctx.service()

    def apply() -> ItemOutcome | DismissOutcome | DeleteOutcome:
        views = Views(ctx)
        item = views.item_by_public_id(public_id)
        if verb == "delete":
            return service.delete(
                principal,
                item_id=item.item_id,
                reason=command.reason,
                discard_undelivered=getattr(command, "discard_undelivered", False),
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        if verb in ("dismiss", "undismiss"):
            return service.dismiss(
                principal,
                item_id=item.item_id,
                reason=command.reason,
                undo=verb == "undismiss",
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        if verb == "abandon":
            return service.abandon(
                principal,
                item.item_id,
                command.reason,
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        if verb == "retry":
            return service.retry(
                principal,
                item.item_id,
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        return service.requeue(
            principal, item.item_id, expected_revision=command.expected_revision, idempotency=pair
        )

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None:
            raise problem from replay

        def reread() -> ItemCommandResult:
            views = Views(ctx)
            return ItemCommandResult(
                item=views.item(views.item_by_public_id(public_id)),
                operation=OperationOut.from_operation(existing),
            )

        return await ctx.call(reread)

    def project() -> ItemCommandResult:
        views = Views(ctx)
        return ItemCommandResult(
            item=views.item(outcome.item or views.item_by_public_id(public_id)),
            operation=OperationOut.from_operation(_operation(ctx, outcome.operation_id)),
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


# -- alerts: several dismissed at once ---------------------------------------------


async def dismiss_all(
    ctx: ApiContext,
    auth: Authenticated,
    body: AttentionDismissRequest,
    pair: tuple[str, str] | None,
) -> AttentionDismissed:
    """Dismiss every alert the request names under one operation. A target
    is skipped, never fatal: an id that names nothing, work that moved
    since the person looked, work that raises no alert."""
    principal = auth.principal
    service = ctx.service()
    # Where each resolved target sits in the request, so an answer by the
    # service's index lands on the entry the client named.
    places: list[int] = []
    results: list[AttentionDismissResult] = [
        AttentionDismissResult(
            item_id=target.item_id,
            run_id=target.run_id,
            outcome="skipped",
            code="not_found",
            detail=NOT_FOUND,
        )
        for target in body.targets
    ]

    def apply() -> DismissAllOutcome:
        views = Views(ctx)
        named: list[tuple[Literal["item", "run"], str, int | None]] = []
        places.clear()
        for place, target in enumerate(body.targets):
            try:
                if target.item_id is not None:
                    item = views.item_by_public_id(target.item_id)
                    named.append(("item", item.item_id, target.expected_revision))
                else:
                    record = views.run_by_public_id(target.run_id or "")
                    named.append(("run", record.run_id, target.expected_revision))
            except Problem:
                continue
            places.append(place)
        return service.dismiss_all(principal, named, reason=body.reason, idempotency=pair)

    def fill(outcomes: list[dict[str, Any]]) -> None:
        for entry in outcomes:
            place = places[int(entry["index"])]
            results[place] = results[place].model_copy(
                update={
                    "outcome": entry["outcome"],
                    "code": entry.get("code"),
                    "detail": entry.get("detail"),
                }
            )

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None:
            raise problem from replay
        fill(list((existing.result or {}).get("results") or []))
        return AttentionDismissed(operation=OperationOut.from_operation(existing), results=results)
    fill([entry.model_dump() for entry in outcome.results])
    op = await ctx.call(lambda: _operation(ctx, outcome.operation_id))
    ctx.hub.notify()
    return AttentionDismissed(operation=OperationOut.from_operation(op), results=results)


# -- runs: cancel, resume, round grants, the review wait ---------------------------

RunVerb = Literal[
    "cancel", "resume", "grant_rounds", "review_resume", "dismiss", "undismiss", "delete"
]

RUN_ACTIONS: dict[RunVerb, str] = {
    "cancel": "run.cancel",
    "resume": "run.resume",
    "grant_rounds": "run.grant_rounds",
    "review_resume": "run.review_resume",
    "dismiss": "run.dismiss",
    "undismiss": "run.undismiss",
    "delete": "run.delete",
}


async def run_verb(
    ctx: ApiContext,
    auth: Authenticated,
    verb: RunVerb,
    public_id: str,
    body: RunCommand | RoundGrant | WorkDeleteCommand | None,
    pair: tuple[str, str] | None,
) -> RunCommandResult:
    """Cancel, resume, grant rounds to, re-arm the review wait of a run, or
    dismiss its alert (and take that back), through the shared service
    verbs ctl and chat use."""
    principal = auth.principal
    service = ctx.service()
    command = body if isinstance(body, RunCommand) else RunCommand()
    grant = body if isinstance(body, RoundGrant) else None
    deletion = body if isinstance(body, WorkDeleteCommand) else WorkDeleteCommand()

    def apply() -> Outcome:
        views = Views(ctx)
        record = views.run_by_public_id(public_id)
        run_id = record.run_id
        if verb == "cancel":
            return service.cancel_run(
                principal,
                run_id,
                retry=command.retry,
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        if verb == "resume":
            return service.resume_run(
                principal, run_id, expected_revision=command.expected_revision, idempotency=pair
            )
        if verb == "delete":
            return service.delete(
                principal,
                run_id=run_id,
                reason=deletion.reason,
                discard_undelivered=deletion.discard_undelivered,
                expected_revision=deletion.expected_revision,
                idempotency=pair,
            )
        if verb in ("dismiss", "undismiss"):
            return service.dismiss(
                principal,
                run_id=run_id,
                reason=command.reason,
                undo=verb == "undismiss",
                expected_revision=command.expected_revision,
                idempotency=pair,
            )
        if verb == "grant_rounds":
            if grant is None:
                raise Problem(422, "invalid_request", "a round grant names its rounds")
            return service.grant_rounds(
                principal,
                run_id,
                grant.rounds,
                expected_revision=grant.expected_revision,
                idempotency=pair,
            )
        return service.resume_review(principal, run_id, idempotency=pair)

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None and existing.state != "running":
            raise problem from replay

        def reread() -> RunCommandResult:
            views = Views(ctx)
            return RunCommandResult(
                run=views.run(views.run_by_public_id(public_id)),
                operation=OperationOut.from_operation(existing),
                message=(existing.result or {}).get("message") if existing.result else None,
            )

        return await ctx.call(reread)

    def project() -> RunCommandResult:
        views = Views(ctx)
        return RunCommandResult(
            run=views.run(views.run_by_public_id(public_id)),
            operation=OperationOut.from_operation(_operation(ctx, outcome.operation_id)),
            message=getattr(outcome, "message", None),
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


# -- steering ------------------------------------------------------------------------


async def steer(
    ctx: ApiContext,
    auth: Authenticated,
    public_id: str,
    body: SteerRequest,
    pair: tuple[str, str] | None,
) -> SteerResult:
    """Explicit direction for the run in flight: a record, then the
    hand-over; the reply settles the record when it lands."""
    principal = auth.principal
    service = ctx.service()
    deadline_s = float(ctx.api.operation_deadline_s)

    def apply() -> Outcome:
        views = Views(ctx)
        run_id = views.run_by_public_id(public_id).run_id
        return service.steer(
            principal,
            run_id,
            body.text,
            source_refs=body.source_refs,
            expected_revision=body.expected_revision,
            deadline_s=deadline_s,
            idempotency=pair,
            task_id=body.task_id,
            agent_slug=body.agent_slug,
        )

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None:
            raise problem from replay

        def reread() -> SteerResult:
            views = Views(ctx)
            record = SteeringStore(views.dstore).for_operation(existing.id)
            if record is None:
                raise not_found()
            return SteerResult(
                steering=views.steering(record), operation=OperationOut.from_operation(existing)
            )

        return await ctx.call(reread)

    def project() -> SteerResult:
        views = Views(ctx)
        record = SteeringStore(views.dstore).get(getattr(outcome, "steering_id", ""))
        if record is None:
            raise not_found()
        return SteerResult(
            steering=views.steering(record),
            operation=OperationOut.from_operation(_operation(ctx, outcome.operation_id)),
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


# -- gates ---------------------------------------------------------------------------


def forge_capability(ctx: ApiContext, repo: str | None) -> Capability:
    """Whether the daemon's forge backend can complete a landing for
    ``repo``: the backend's own answer for the capability the approve
    path relies on (reading the base's required checks), ``UNKNOWN``
    for a forge with no backend yet, ``UNSUPPORTED`` with no forge
    handle at all. Read from the backend's static table — never by
    provisioning a sandbox in the request path."""
    loop: Any = ctx.loop
    if getattr(loop, "github", None) is None:
        return Capability.UNSUPPORTED
    kind = ctx.config.vcs_kind_for(repo)
    if kind != "github":
        return Capability.UNKNOWN
    from lantern.vcs.github.ops import GithubOps

    table = getattr(GithubOps, "CAPABILITIES", {})
    return Capability(table.get("required_checks_introspection", Capability.UNKNOWN))


async def approve_gate(
    ctx: ApiContext,
    auth: Authenticated,
    public_id: str,
    body: GateApproval,
    pair: tuple[str, str] | None,
) -> GateResult:
    """Endorse and release one gate at exactly the revision the person
    saw: the eligibility (kind, state, the forge's ability to act) is
    checked here, the revision-bound swap in the loop."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        views = Views(ctx)
        gate = views.gate_by_public_id(public_id)
        # A replay is answered from its record before the gate is judged
        # again: the approval it repeats is what moved the gate, and the
        # gate as it stands now would refuse it.
        store = getattr(ctx.loop, "operations", None)
        earlier = (
            store.for_idempotency(*pair) if isinstance(store, OperationStore) and pair else None
        )
        if (
            earlier is not None
            and earlier.action == "gate.approve"
            and earlier.target_key == gate.run_id
        ):
            raise OperationReplay(earlier)
        run = views.run_record(gate.run_id)
        forge = forge_capability(ctx, gate.repo or None) if gate.kind == "merge" else None
        check_eligibility(
            "gate_approve",
            Subject(
                run_kind=run.kind if run is not None else "code",
                run_state=run.state if run is not None else None,
                gate_state=gate.state,
                forge=forge,
            ),
        )
        return service.approve_gate(
            principal, gate.run_id, expected_revision=body.expected_revision, idempotency=pair
        )

    try:
        outcome = await run_command(ctx, apply)
    except Replayed as replay:
        existing = replay.operation
        problem = replayed_problem(existing)
        if problem is not None and existing.state != "running":
            raise problem from replay

        def reread() -> GateResult:
            views = Views(ctx)
            return GateResult(
                gate=views.gate(views.gate_by_public_id(public_id)),
                operation=OperationOut.from_operation(existing),
                message=(existing.result or {}).get("message"),
            )

        return await ctx.call(reread)

    def project() -> GateResult:
        views = Views(ctx)
        return GateResult(
            gate=views.gate(views.gate_by_public_id(public_id)),
            operation=OperationOut.from_operation(_operation(ctx, outcome.operation_id)),
            message=getattr(outcome, "message", None),
        )

    result = await ctx.call(project)
    ctx.hub.notify()
    return result


#: The actions a typed command frame may name, with the capability each
#: needs and the canonical route its idempotency scope is keyed on.
ACTIONS: dict[str, tuple[str, str]] = {
    "item.admit": ("items:create", "/v1/items"),
    "item.retry": ("runs:control", "/v1/items/{id}/retry"),
    "item.requeue": ("runs:control", "/v1/items/{id}/requeue"),
    "item.abandon": ("runs:control", "/v1/items/{id}/abandon"),
    "item.dismiss": ("runs:control", "/v1/items/{id}/dismiss"),
    "item.undismiss": ("runs:control", "/v1/items/{id}/undismiss"),
    "item.delete": ("runs:control", "/v1/items/{id}/delete"),
    "run.cancel": ("runs:control", "/v1/runs/{id}/cancel"),
    "run.resume": ("runs:control", "/v1/runs/{id}/resume"),
    "run.dismiss": ("runs:control", "/v1/runs/{id}/dismiss"),
    "run.undismiss": ("runs:control", "/v1/runs/{id}/undismiss"),
    "run.delete": ("runs:control", "/v1/runs/{id}/delete"),
    "run.steer": ("runs:steer", "/v1/runs/{id}/steering"),
    "run.grant_rounds": ("budgets:grant", "/v1/runs/{id}/round-grants"),
    "run.review_resume": ("runs:control", "/v1/runs/{id}/review-wait/resume"),
    "gate.approve": ("gates:approve", "/v1/gates/{id}/approve"),
}


async def dispatch(
    ctx: ApiContext,
    auth: Authenticated,
    *,
    action: str,
    target: str | None,
    params: dict[str, Any],
    idempotency_key: str | None,
    expected_revision: int | None,
) -> tuple[dict[str, Any], Callable[[], None] | None]:
    """A typed command frame (the WebSocket's), routed to the same function
    the REST route calls; the result as the REST body would be, and the
    effect a stop or restart defers until the reply is on its way."""
    from lantern.api.admin import ADMIN_ACTIONS, run_admin

    spec = ACTIONS.get(action) or ADMIN_ACTIONS.get(action)
    if spec is None:
        raise Problem(422, "unknown_action", f"no such action {action!r}")
    capability, route = spec
    if not auth.principal.can(capability):  # type: ignore[arg-type]
        raise Problem(
            403, "forbidden", f"{auth.principal.id} lacks {capability}", capability=capability
        )
    if action in ADMIN_ACTIONS:
        pair = idempotency_pair(
            auth.principal, idempotency_key, route.replace("{id}", target or ""), required=False
        )
        return await run_admin(ctx, auth, action=action, target=target, params=params, pair=pair)
    if action == "item.admit":
        from pydantic import TypeAdapter, ValidationError

        try:
            body: IssueIntake | WorkloadIntake | ToolIntake = TypeAdapter(
                IntakeRequest
            ).validate_python(params)
        except ValidationError as exc:
            raise Problem(
                422,
                "invalid_request",
                "the command's params are not a valid admission",
                errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
            ) from exc
        pair = idempotency_pair(auth.principal, idempotency_key, route, required=True)
        admitted = await admit(ctx, auth, body, pair)
        return admitted.model_dump(mode="json"), None
    if not target:
        raise Problem(422, "invalid_request", f"{action} needs a target")
    pair = idempotency_pair(
        auth.principal, idempotency_key, route.replace("{id}", target), required=False
    )
    from pydantic import ValidationError

    try:
        if action.startswith("item."):
            verb: ItemVerb = action.removeprefix("item.")  # type: ignore[assignment]
            command: ItemCommand | WorkDeleteCommand = (
                WorkDeleteCommand(
                    reason=params.get("reason"),
                    expected_revision=expected_revision,
                    discard_undelivered=bool(params.get("discard_undelivered", False)),
                )
                if verb == "delete"
                else ItemCommand(reason=params.get("reason"), expected_revision=expected_revision)
            )
            return (
                (await item_command(ctx, auth, verb, target, command, pair)).model_dump(
                    mode="json"
                ),
                None,
            )
        if action == "run.steer":
            request = SteerRequest(
                text=str(params.get("text") or ""),
                source_refs=list(params.get("source_refs") or []),
                expected_revision=expected_revision,
            )
            return (await steer(ctx, auth, target, request, pair)).model_dump(mode="json"), None
        if action == "gate.approve":
            if expected_revision is None:
                raise Problem(422, "invalid_request", "gate.approve needs expected_revision")
            approval = GateApproval(expected_revision=expected_revision)
            return (
                (await approve_gate(ctx, auth, target, approval, pair)).model_dump(mode="json"),
                None,
            )
        run_verb_name: RunVerb = action.removeprefix("run.")  # type: ignore[assignment]
        run_body: RunCommand | RoundGrant | WorkDeleteCommand
        if run_verb_name == "delete":
            run_body = WorkDeleteCommand(
                reason=params.get("reason"),
                expected_revision=expected_revision,
                discard_undelivered=bool(params.get("discard_undelivered", False)),
            )
        elif run_verb_name == "grant_rounds":
            run_body = RoundGrant(
                rounds=int(params.get("rounds") or 0), expected_revision=expected_revision
            )
        else:
            run_body = RunCommand(
                reason=params.get("reason"),
                retry=bool(params.get("retry", False)),
                expected_revision=expected_revision,
            )
        return (
            (await run_verb(ctx, auth, run_verb_name, target, run_body, pair)).model_dump(
                mode="json"
            ),
            None,
        )
    except ValidationError as exc:
        raise Problem(
            422,
            "invalid_request",
            "the command's params are not valid",
            errors=[{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()],
        ) from exc
