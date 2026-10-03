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

from lantern.api.admin import resume_repository
from lantern.api.attention import (
    REPOSITORY_ACTIONS,
    TASK_ACTIONS,
    WORK_COMMANDS,
    capability_for,
    find,
)
from lantern.api.auth.deps import Authenticated
from lantern.api.commands import approve_gate, item_command, run_verb, steer
from lantern.api.context import ApiContext
from lantern.api.errors import Problem
from lantern.api.models import (
    ApiModel,
    AttentionEntry,
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
from lantern.api.plan_schemas import EpicRunChanged
from lantern.api.projections import Views, not_found
from lantern.api.publicids import run_public_id
from lantern.api.routes.plan_runs import ControlVerb, control_epic_run
from lantern.daemon.controls.operations import Operation, OperationStore


class AttentionActRequest(ApiModel):
    """One action on one entry. ``action`` is a name the entry lists in
    ``actions``; ``expected_revision`` is the entry's ``revision`` as the
    person read it; ``params`` are the action's own arguments, the fields
    its own route takes in its body."""

    action: str = Field(min_length=1, max_length=64)
    expected_revision: int | None = Field(default=None, ge=0)
    params: dict[str, Any] = Field(default_factory=dict)


#: What an act answers with: the body the action's own route answers.
ActOutcome = (
    ItemCommandResult
    | RunCommandResult
    | SteerResult
    | GateResult
    | RepositoryResult
    | EpicRunChanged
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
    route = _ROUTES.get(action)
    if route is None:
        raise Problem(422, "unknown_action", f"no such action {action!r}", actions=sorted(_ROUTES))
    capability = capability_for(action)
    if not auth.principal.can(capability):  # type: ignore[arg-type]
        raise Problem(
            403, "forbidden", f"{auth.principal.id} lacks {capability}", capability=capability
        )
    kind = entry_id.partition(":")[0]
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
