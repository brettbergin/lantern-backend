"""Plans: initiatives, epics and tasks, drafted here and published to the
forge one level at a time (#2334).

Feature ``planning``. Every plan, drafts included, is readable by anyone
holding ``runs:read``; drafting, editing, asking the planner for a
breakdown and answering its clarifying questions (feature
``planning.clarify``) need ``plans:create``. Every mutation names the
``expected_revision`` it read; a stale one is ``409 stale_revision`` with
the plan's current revision.

After publish the forge wins (#2342): reading a plan folds in what changed
on the forge when its last reading is stale, ``POST .../sync`` does it now,
and ``POST .../drift/ack`` marks the forge's changes seen. A person's direct
writes (#2350) need ``plans:publish``: ``PATCH`` of a published node's
sections writes its issue, refused when the issue changed since it was
read; ``POST .../attach`` and ``.../detach`` link and unlink a child.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response

from lantern.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from lantern.api.commands import admit_plan, idempotency, replayed_problem
from lantern.api.context import ApiContext
from lantern.api.errors import Problem
from lantern.api.models import rfc3339
from lantern.api.pagination import Page, decode_cursor, encode_cursor
from lantern.api.plan_schemas import (
    PlanAnswerOut,
    PlanAnswers,
    PlanAnswersAccepted,
    PlanApprove,
    PlanAttach,
    PlanAttached,
    PlanBreakdown,
    PlanBreakdownAccepted,
    PlanChoiceOut,
    PlanCreate,
    PlanDeleted,
    PlanDetach,
    PlanDrift,
    PlanDriftAck,
    PlanForge,
    PlanGenerationOut,
    PlanInput,
    PlanNodeCreate,
    PlanNodeOut,
    PlanNodeUpdate,
    PlanOut,
    PlanPublish,
    PlanPublished,
    PlanPublishResult,
    PlanQuestionOut,
    PlanReplanApplied,
    PlanReplanApprove,
    PlanReplanDiscard,
    PlanReplanEntryOut,
    PlanReplanOut,
    PlanReplanResult,
    PlanRollup,
    PlanSummary,
    PlanUpdate,
)
from lantern.api.publicids import run_public_id
from lantern.daemon.controls.intake import PlanAdmission
from lantern.daemon.controls.operations import (
    IdempotencyConflict,
    Operation,
    OperationSpec,
    OperationStore,
)
from lantern.engine.planning import Clarification, PlanAnswer
from lantern.plans import Plan, PlanNode, PlanRefusal
from lantern.plans.model import content_version
from lantern.plans.reconcile import Reconciliation
from lantern.plans.service import SECTIONS

router = APIRouter(prefix="/v1/plans", tags=["plans"])

PROBLEM = {"description": "A refusal, as `application/problem+json`."}


def problem_of(exc: PlanRefusal) -> Problem:
    return Problem(exc.status, exc.code, exc.detail, **exc.extra)


def actor_of(auth: Authenticated) -> dict[str, Any]:
    principal = auth.principal
    return {
        "kind": principal.kind,
        "id": principal.id,
        "display": principal.display or principal.id,
        "via": "api",
    }


def _sections(body: Any) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    return {key: value for key, value in fields.items() if key in SECTIONS}


def node_out(node: PlanNode) -> PlanNodeOut:
    return PlanNodeOut(
        id=node.id,
        parent_id=node.parent_id,
        position=node.position,
        level=node.level,
        repository=node.repository,
        state=node.state,
        origin=node.origin,
        title=node.title,
        goal=node.goal,
        context=node.context,
        acceptance_criteria=list(node.acceptance_criteria),
        kind=node.kind,
        workload_profile=node.workload_profile,
        verify_commands=list(node.verify_commands),
        depends_on=list(node.depends_on),
        non_goals=node.non_goals,
        constraints=node.constraints,
        forge=(
            None
            if node.forge is None
            else PlanForge(
                number=node.forge.number,
                url=node.forge.url,
                state=node.forge.state,
                version=content_version(node) if node.state == "published" else None,
                updated_at=node.forge.updated_at,
                detached=node.forge.detached,
                marker_missing=node.forge.marker_missing,
                checklist_error=node.forge.checklist_error,
            )
        ),
        generation=None if node.generation is None else generation_out(node.generation),
        created_at=rfc3339(node.created_at) or "",
        updated_at=rfc3339(node.updated_at) or "",
        drift=[
            PlanDrift(
                change=entry.change,
                at=rfc3339(entry.at) or "",
                before=dict(entry.before),
                after=dict(entry.after),
                reason=entry.reason,
            )
            for entry in node.drift
        ],
        replan=(
            None
            if node.replan is None
            else PlanReplanOut(
                id=node.replan.id,
                run_id=node.replan.run_id,
                proposed_at=rfc3339(node.replan.proposed_at) or "",
                entries=[
                    PlanReplanEntryOut(
                        id=e.id,
                        action=e.action,
                        node_id=e.node_id,
                        sections=dict(e.sections),
                        before=dict(e.before),
                        rationale=e.rationale,
                        error=e.error,
                        forge_version=e.forge_version,
                    )
                    for e in node.replan.entries
                ],
            )
        ),
    )


def generation_out(found: Clarification) -> PlanGenerationOut:
    return PlanGenerationOut(
        run_id=run_public_id(found.run_id),
        status=found.status,
        questions=[
            PlanQuestionOut(
                id=q.id,
                prompt=q.prompt,
                choices=[
                    PlanChoiceOut(value=c.value, label=c.label, description=c.description)
                    for c in q.choices
                ],
                allow_free_text=q.allow_free_text,
            )
            for q in found.questions
        ],
        answers={
            qid: PlanAnswerOut(value=a.value, text=a.text) for qid, a in found.answers.items()
        },
        asked_at=rfc3339(found.asked_at) or "",
        answered_at=rfc3339(found.answered_at) if found.answered_at is not None else None,
        answered_by=found.answered_by,
    )


def _rollup(plan: Plan) -> PlanRollup:
    tasks = [n for n in plan.nodes if n.level == "task"]
    return PlanRollup(
        epics=sum(1 for n in plan.nodes if n.level == "epic"),
        tasks=len(tasks),
        tasks_closed=sum(1 for n in tasks if n.forge is not None and n.forge.state == "closed"),
        published=sum(1 for n in plan.nodes if n.state == "published"),
    )


def _summary_fields(plan: Plan) -> dict[str, Any]:
    root = plan.root
    return {
        "id": plan.id,
        "workspace_id": plan.workspace_id,
        "title": str(plan.input.get("title", root.title))
        if plan.generation_pending
        else root.title,
        "level": root.level,
        "repository": root.repository,
        "state": plan.state,
        "revision": plan.revision,
        "root_id": plan.root_id,
        "created_by": plan.created_by,
        "created_by_display": plan.created_by_display,
        "created_at": rfc3339(plan.created_at) or "",
        "updated_at": rfc3339(plan.updated_at) or "",
        "rollup": _rollup(plan),
        "drift": sum(1 for n in plan.nodes if n.drift),
        "reconciled_at": rfc3339(plan.reconciled_at),
        "reconcile_error": plan.reconcile_error,
    }


def plan_out(plan: Plan, *, reconcile_error: str | None = None) -> PlanOut:
    fields = _summary_fields(plan)
    if reconcile_error is not None:
        fields["reconcile_error"] = reconcile_error
    return PlanOut(
        **fields,
        nodes=[node_out(n) for n in plan.nodes],
        input=PlanInput.model_validate(plan.input) if plan.input else None,
        generation_pending=plan.generation_pending,
    )


def _reconciled(result: Reconciliation) -> PlanOut:
    return plan_out(result.plan, reconcile_error=result.error)


def _forge_args(ctx: ApiContext) -> dict[str, Any]:
    """How the plan service reaches the daemon's forge connection."""
    forge = ctx.loop.github
    return {
        "forge_kind": None if forge is None else str(forge.kind),
        "connect": lambda: forge.call(lambda ops: ops),
        "clock": ctx.clock,
    }


@router.get("", response_model=Page[PlanSummary], summary="List plans")
async def list_plans(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    repository: Annotated[str | None, Query(max_length=200)] = None,
    level: Annotated[Literal["initiative", "epic"] | None, Query()] = None,
    state: Annotated[Literal["draft", "published", "archived"] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 200,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[PlanSummary]:
    """Every plan in the workspace, drafts included, most recently changed
    first, a page at a time: at most ``limit`` (200, the default, is the
    most), and ``cursor`` — the ``next_cursor`` of the page before — for
    the rest. ``repository`` matches a plan any of whose nodes targets it;
    a cursor belongs to the filters it was issued under (``400
    invalid_cursor`` otherwise)."""
    filters: dict[str, Any] = {"repository": repository, "level": level, "state": state}
    after: tuple[float, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (float(key["u"]), str(key["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc
    plans = await ctx.call(ctx.plans.find, repository=repository, level=level, state=state)
    ordered = sorted(plans, key=lambda plan: (-plan.updated_at, plan.id))
    if after is not None:
        ordered = [p for p in ordered if (-p.updated_at, p.id) > (-after[0], after[1])]
    more = len(ordered) > limit
    page = ordered[:limit]
    next_cursor = (
        encode_cursor({"u": page[-1].updated_at, "i": page[-1].id}, filters)
        if more and page
        else None
    )
    return Page(
        data=[PlanSummary(**_summary_fields(plan)) for plan in page],
        next_cursor=next_cursor,
        has_more=more,
    )


@router.post(
    "",
    response_model=PlanOut,
    status_code=201,
    summary="Draft a plan",
    responses={409: PROBLEM, 422: PROBLEM},
)
async def create_plan(
    body: PlanCreate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """A new draft plan from the form. A repository whose forge cannot hold
    a plan is ``409 planning_unsupported``, naming why."""
    try:
        plan = await ctx.call(
            ctx.plans.create,
            level=body.level,
            repository=body.repository,
            sections=_sections(body),
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.get("/{plan_id}", response_model=PlanOut, summary="Read a plan", responses={404: PROBLEM})
async def get_plan(
    plan_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> PlanOut:
    """The plan and every node. A plan with anything on the forge whose
    last reading is older than ``[planning] reconcile_interval_s`` is
    reconciled first; a forge that cannot be read never fails the read —
    the stored plan is served with ``reconciled_at`` and the reason in
    ``reconcile_error``."""
    forge = ctx.loop.github
    try:
        result = await ctx.call(
            ctx.plans.open,
            plan_id,
            ready=bool(getattr(forge, "provisioned", True)),
            actor=actor_of(auth),
            **_forge_args(ctx),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    if result.changes:
        ctx.hub.notify()
    return _reconciled(result)


@router.post(
    "/{plan_id}/sync",
    response_model=PlanOut,
    summary="Reconcile a plan from the forge now",
    responses={404: PROBLEM, 409: PROBLEM, 503: PROBLEM},
)
async def sync_plan(
    plan_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Read the plan's tree on the forge now and fold it in: titles,
    sections under the rendered headings, open or closed, children adopted,
    moved or detached — each change a ``plan.drift`` event. Never writes to
    the forge. A forge that cannot be reached at all is ``503
    source_unavailable``; issues it would not answer are named in
    ``reconcile_error``. An archived plan is ``409 plan_archived``, one
    being published or read right now ``409 already_in_progress``."""
    try:
        result = await ctx.call(
            ctx.plans.reconcile, plan_id, actor=actor_of(auth), force=True, **_forge_args(ctx)
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    if result.changes:
        ctx.hub.notify()
    return _reconciled(result)


@router.post(
    "/{plan_id}/drift/ack",
    response_model=PlanOut,
    summary="Mark the forge's changes to a plan seen",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def ack_drift(
    plan_id: str,
    body: PlanDriftAck,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Clear the drift of every node, or of the nodes ``node_ids`` names:
    someone looked, and the next forge edit is diffed against what they
    saw. Nothing to clear changes nothing."""
    try:
        plan = await ctx.call(
            ctx.plans.ack_drift,
            plan_id,
            expected_revision=body.expected_revision,
            node_ids=body.node_ids,
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.patch(
    "/{plan_id}",
    response_model=PlanOut,
    summary="Edit a plan",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def update_plan(
    plan_id: str,
    body: PlanUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Edit the plan's own sections (its root node's)."""
    try:
        plan = await ctx.call(
            ctx.plans.update,
            plan_id,
            expected_revision=body.expected_revision,
            sections=_sections(body),
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.delete(
    "/{plan_id}",
    response_model=PlanDeleted,
    summary="Delete a draft plan or archive a published one",
    responses={404: PROBLEM, 409: PROBLEM},
)
async def delete_plan(
    plan_id: str,
    expected_revision: Annotated[int, Query(ge=1)],
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanDeleted:
    """A plan with nothing on the forge is deleted; one with anything
    published is archived (its issues stay where they are)."""
    try:
        outcome = await ctx.call(
            ctx.plans.delete,
            plan_id,
            expected_revision=expected_revision,
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return PlanDeleted(id=plan_id, outcome=outcome)  # type: ignore[arg-type]


@router.post(
    "/{plan_id}/nodes",
    response_model=PlanOut,
    status_code=201,
    summary="Add a node to a plan",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def add_node(
    plan_id: str,
    body: PlanNodeCreate,
    response: Response,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """A new child one level under ``parent_id``; the plan as it now is,
    with the new node's id in the ``Location`` header. A parent at
    ``[planning]``'s cap refuses it (``409 level_full``) — the same count
    the planner's room, attaching and publishing are held to."""
    try:
        plan, node_id = await ctx.call(
            ctx.plans.add_node,
            plan_id,
            expected_revision=body.expected_revision,
            parent_id=body.parent_id,
            repository=body.repository,
            sections=_sections(body),
            position=body.position,
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    response.headers["Location"] = f"/v1/plans/{plan_id}/nodes/{node_id}"
    ctx.hub.notify()
    return plan_out(plan)


@router.patch(
    "/{plan_id}/nodes/{node_id}",
    response_model=PlanOut,
    summary="Edit or move a node",
    responses={
        403: PROBLEM,
        404: PROBLEM,
        409: PROBLEM,
        422: PROBLEM,
        502: PROBLEM,
        503: PROBLEM,
    },
)
async def update_node(
    plan_id: str,
    node_id: str,
    body: PlanNodeUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Edit a node's sections or move it among its siblings. Editing a
    proposed or approved node makes it a draft again.

    Editing a published node's sections writes them to its issue at once
    and needs ``plans:publish`` (``403 forbidden`` naming it otherwise):
    only the title (when changed) and the sections edited are rewritten —
    a person's text outside the rendered headings, the other sections, the
    marker and the managed checklist stay. ``forge_version`` names the
    version of the issue the client read; the issue is read first and, when
    it changed on the forge since, the edit is ``409 forge_changed`` with
    the forge's version in ``forge_version`` and ``current`` (its title and
    sections as read) and nothing is written. The edit is recorded as
    ``plan.node.changed`` with ``change: issue_edited``."""
    sections = _sections(body)
    actor = actor_of(auth)

    def run() -> Plan:
        node = ctx.plans.get(plan_id).node(node_id)
        if node is None or node.state != "published" or not sections:
            return ctx.plans.update_node(
                plan_id,
                node_id,
                expected_revision=body.expected_revision,
                sections=sections,
                position=body.position,
                now=ctx.clock(),
                actor=actor,
            )
        if not auth.principal.can("plans:publish"):
            raise PlanRefusal(
                403,
                "forbidden",
                f"{auth.principal.id} lacks plans:publish: an edit of a published node "
                "writes its issue",
                capability="plans:publish",
            )
        return ctx.plans.edit_published(
            plan_id,
            node_id,
            expected_revision=body.expected_revision,
            forge_version=body.forge_version,
            sections=sections,
            position=body.position,
            actor=actor,
            **_forge_args(ctx),
        )

    try:
        plan = await ctx.call(run)
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.post(
    "/{plan_id}/nodes/{node_id}/attach",
    response_model=PlanAttached,
    summary="Attach an existing issue as a child",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
async def attach_issue(
    plan_id: str,
    node_id: str,
    body: PlanAttach,
    response: Response,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> PlanAttached:
    """Link an existing open issue — ``repository`` and ``number``, or its
    ``url`` — as a child one level under the node: a sub-issue on GitHub, a
    line in the parent's managed checklist on GitLab, and its level label
    (never the trigger or the workload label). It is recorded ``published``
    with ``origin: forge`` and its sections read from its body; the
    ``Location`` header names its node. A closed issue (``409
    issue_closed``), a pull request (``422 not_an_issue``), an issue
    already in this plan (``409 already_in_plan``) or another (``409
    in_another_plan``), one already under another parent on GitHub (``409
    already_has_parent``), a task outside its epic's repository (``422``)
    and a parent at its cap (``409 too_many_children``) are refused."""
    try:
        attached = await ctx.call(
            ctx.plans.attach,
            plan_id,
            node_id,
            expected_revision=body.expected_revision,
            repository=body.repository,
            number=body.number,
            url=body.url,
            actor=actor_of(auth),
            **_forge_args(ctx),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    response.headers["Location"] = f"/v1/plans/{plan_id}/nodes/{attached.node_id}"
    ctx.hub.notify()
    return PlanAttached(
        plan=plan_out(attached.plan),
        node_id=attached.node_id,
        linked=attached.linked,
        reason=attached.reason,
    )


@router.post(
    "/{plan_id}/nodes/{node_id}/detach",
    response_model=PlanOut,
    summary="Detach a child from its parent",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 502: PROBLEM, 503: PROBLEM},
)
async def detach_issue(
    plan_id: str,
    node_id: str,
    body: PlanDetach,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> PlanOut:
    """Unlink a published child from its parent on the forge — its
    sub-issue link and any checklist line — without closing its issue. The
    node stays in the plan with ``forge.detached`` saying a person did it,
    is no longer followed (its subtree is left as it was), and its siblings
    stop depending on it."""
    try:
        plan = await ctx.call(
            ctx.plans.detach,
            plan_id,
            node_id,
            expected_revision=body.expected_revision,
            actor=actor_of(auth),
            **_forge_args(ctx),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.delete(
    "/{plan_id}/nodes/{node_id}",
    response_model=PlanOut,
    summary="Remove a node",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def remove_node(
    plan_id: str,
    node_id: str,
    expected_revision: Annotated[int, Query(ge=1)],
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Remove an unpublished node and everything under it; a published one
    is ``409 node_published`` (detach it instead)."""
    try:
        plan = await ctx.call(
            ctx.plans.remove_node,
            plan_id,
            node_id,
            expected_revision=expected_revision,
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.post(
    "/{plan_id}/nodes/{node_id}/approve",
    response_model=PlanOut,
    summary="Approve a node's children",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def approve_children(
    plan_id: str,
    node_id: str,
    body: PlanApprove,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """A person's "this is right": the node's draft and proposed children —
    every one, or those ``node_ids`` names — become ``approved``, ready to
    publish."""
    try:
        plan = await ctx.call(
            ctx.plans.approve,
            plan_id,
            node_id,
            expected_revision=body.expected_revision,
            node_ids=body.node_ids,
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


PUBLISH_ACTION = "plan.publish"


def replayed_refusal(op: Operation) -> None:
    """What an earlier call under the same key recorded, when that was a
    refusal (raised again with its status and code) or the call still
    runs (``409``); nothing when it succeeded and its result replays."""
    if op.state == "failed" and op.result and "status" in op.result:
        raise Problem(
            int(op.result["status"]),
            op.error_code or "failed",
            op.error_detail or "the earlier attempt was refused",
            **dict(op.result.get("extra") or {}),
            operation_id=op.id,
        )
    problem = replayed_problem(op)
    if problem is not None:
        raise problem


def recorded[R, T](
    ctx: ApiContext,
    spec: OperationSpec,
    *,
    replay: Callable[[Operation], T],
    call: Callable[[], R],
    result: Callable[[R], dict[str, Any]],
    out: Callable[[R, str], T],
) -> T:
    """One write to the plan or the forge as a recorded operation: accept
    ``spec`` under its idempotency key (a replay answers ``replay`` of the
    earlier operation; a different request under the same key is ``409
    idempotency_conflict``), claim it, make the ``call``, and finish the
    operation as succeeded with ``result`` of what came back — or failed
    with the refusal (raised on as a ``Problem`` naming the operation) or
    the crash. Every operation route in this module and the epic runs'
    comes here, so a daemon that dies mid-call leaves the same record
    whichever route was running."""
    store = getattr(ctx.loop, "operations", None)
    if not isinstance(store, OperationStore):
        raise Problem(503, "daemon_not_ready", "the daemon keeps no operation record")
    try:
        op, created = store.accept(spec, ctx.clock())
    except IdempotencyConflict as exc:
        raise Problem(
            409,
            "idempotency_conflict",
            "the idempotency key was already used with a different request",
            operation_id=exc.existing.id,
        ) from exc
    if not created:
        try:
            return replay(op)
        except PlanRefusal as exc:
            raise problem_of(exc) from exc
    store.claim(op.id, getattr(ctx.loop, "generation", None), ctx.clock())
    try:
        value = call()
    except PlanRefusal as exc:
        store.finish(
            op.id,
            ctx.clock(),
            state="failed",
            result={"status": exc.status, "extra": exc.extra},
            error_code=exc.code,
            error_detail=exc.detail,
        )
        raise Problem(exc.status, exc.code, exc.detail, **exc.extra, operation_id=op.id) from exc
    except Exception as exc:
        store.finish(
            op.id,
            ctx.clock(),
            state="failed",
            error_code="crashed",
            error_detail=f"{type(exc).__name__}: {exc}"[:2000],
        )
        raise
    store.finish(op.id, ctx.clock(), state="succeeded", result=result(value))
    return out(value, op.id)


def _published(
    plan: Plan, results: list[dict[str, Any]], op_id: str, *, replayed: bool
) -> PlanPublished:
    return PlanPublished(
        plan=plan_out(plan),
        results=[PlanPublishResult(**r) for r in results],
        operation_id=op_id,
        replayed=replayed,
    )


def _replay(ctx: ApiContext, plan_id: str, op: Operation) -> PlanPublished:
    """An earlier call under the same key: its results and the plan as it
    is now, or the refusal it recorded, or ``409`` while it still runs."""
    replayed_refusal(op)
    try:
        plan = ctx.plans.get(plan_id)
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    return _published(plan, list((op.result or {}).get("results") or []), op.id, replayed=True)


@router.post(
    "/{plan_id}/nodes/{node_id}/publish",
    response_model=PlanPublished,
    summary="Publish a node's level to the forge",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 503: PROBLEM},
)
async def publish_children(
    plan_id: str,
    node_id: str,
    body: PlanPublish,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> PlanPublished:
    """Write the node's approved children — and the node itself first when
    it is not on the forge yet — as issues with their level label and the
    ``sbx-plan`` marker, linked under their parent. Never the trigger or
    the workload label. A node that fails is reported and left as it was;
    repeating the call resumes and duplicates nothing. The
    ``Idempotency-Key`` header is required: a replay answers the same
    results, a different body under the same key is ``409
    idempotency_conflict``."""
    principal = auth.principal
    pair = idempotency(
        request, principal, f"/v1/plans/{plan_id}/nodes/{node_id}/publish", required=True
    )
    actor = actor_of(auth)

    def run() -> PlanPublished:
        forge = ctx.loop.github
        return recorded(
            ctx,
            OperationSpec(
                action=PUBLISH_ACTION,
                target_kind="plan",
                target_key=plan_id,
                principal=principal,
                request={
                    "plan_id": plan_id,
                    "node_id": node_id,
                    "expected_revision": body.expected_revision,
                },
                idempotency=pair,
                expected_revision=body.expected_revision,
            ),
            replay=lambda op: _replay(ctx, plan_id, op),
            call=lambda: ctx.plans.publish(
                plan_id,
                node_id,
                expected_revision=body.expected_revision,
                forge_kind=None if forge is None else str(forge.kind),
                connect=lambda: forge.call(lambda ops: ops),
                clock=ctx.clock,
                actor=actor,
            ),
            result=lambda level: {
                "results": [r.as_dict() for r in level.results],
                "revision": level.plan.revision,
            },
            out=lambda level, op_id: _published(
                level.plan, [r.as_dict() for r in level.results], op_id, replayed=False
            ),
        )

    published = await ctx.call(run)
    ctx.hub.notify()
    return published


@router.post(
    "/{plan_id}/nodes/{node_id}/breakdown",
    response_model=PlanBreakdownAccepted,
    status_code=202,
    summary="Propose a node's next level",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def breakdown_node(
    plan_id: str,
    node_id: str,
    body: PlanBreakdown,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanBreakdownAccepted:
    """Start a ``plan`` run that reads a read-only checkout of the node's
    repository and proposes its next level — an initiative's epics or an
    epic's tasks, at most the level's cap. The run is queued like any work
    (it appears in the queue and History, can be cancelled, and answers to
    ``channel_id`` when one is named); ``202`` names its work item, whose
    ``run_id`` is set once it is dispatched. The proposal is delivered to
    the plan, never to the forge: it replaces the node's previous
    ``proposed`` children and arrives as ``plan.generation.proposed``; a
    run that proposes nothing ends with ``plan.generation.failed``.

    A node on the forge with children there is re-planned (#2346): the
    run is told every current child, as the forge has them when it starts,
    and proposes a diff — children to add, to change, to close — that
    waits on the node (``replan``) for ``.../replan/approve`` or
    ``.../replan/discard``, and arrives as ``plan.generation.proposed``
    with ``kind: "replan"``. Nothing reaches the forge before approval.

    Refused: a task (``422``: it has no children); a repository planning
    is off for (``409 planning_unsupported``); a level at its cap, for a
    breakdown (``409 level_full``); a breakdown of the node already queued
    or running (``409 already_in_progress``); a stale
    ``expected_revision``."""

    def check() -> None:
        ctx.plans.breakdown_target(plan_id, node_id, expected_revision=body.expected_revision)

    try:
        await ctx.call(check)
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    pair = idempotency(
        request, auth.principal, f"/v1/plans/{plan_id}/nodes/{node_id}/breakdown", required=False
    )
    admitted = await admit_plan(
        ctx,
        auth,
        PlanAdmission(
            plan_id=plan_id,
            node_id=node_id,
            expected_revision=body.expected_revision,
            note=body.note or "",
            channel_id=body.channel_id,
        ),
        pair,
    )
    return PlanBreakdownAccepted(
        plan_id=plan_id,
        node_id=node_id,
        item=admitted.item,
        operation=admitted.operation,
        created=admitted.created,
    )


@router.post(
    "/{plan_id}/nodes/{node_id}/answers",
    response_model=PlanAnswersAccepted,
    summary="Answer or skip a breakdown's clarifying questions",
    responses={403: PROBLEM, 404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def answer_node(
    plan_id: str,
    node_id: str,
    body: PlanAnswers,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanAnswersAccepted:
    """Feature ``planning.clarify``. Answer the questions a breakdown of
    the node asked before it proposes (the node's ``generation``, while its
    ``status`` is ``awaiting_answers``), keyed by question id — each a
    choice's ``value``, or ``text`` in the person's own words where the
    question allows it — or ``skip`` them to let the planner decide. The
    answers are recorded on the plan (``plan.generation.answered``) and
    the run waiting on them goes back to the queue: it proposes with the
    answers in its prompt. The same questions can be answered from the
    run's chat thread; whichever answer settles them first wins.

    Refused: nothing waiting on the node (``409 no_questions``); questions
    already answered or skipped (``409 already_answered``); the run that
    asked them no longer waiting (``409 not_awaiting_answers``); an answer
    to a question that was not asked, a value that is not one of its
    choices, text for a question that takes only its choices, answers and
    ``skip`` together, or neither (``422``); a stale ``expected_revision``."""
    answers = {
        qid: PlanAnswer(value=answer.value, text=answer.text or "")
        for qid, answer in body.answers.items()
    }

    def apply() -> Any:
        return ctx.loop.answer_plan_questions(
            plan_id,
            node_id,
            answers=answers,
            skip=body.skip,
            actor=actor_of(auth),
            settle=True,
            expected_revision=body.expected_revision,
        )

    try:
        outcome = await ctx.call(apply)
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return PlanAnswersAccepted(
        plan=plan_out(outcome.plan),
        run_id=run_public_id(outcome.clarification.run_id),
        resumed=outcome.resumed,
    )


REPLAN_ACTION = "plan.replan.approve"


def _replan_applied(
    plan: Plan, results: list[dict[str, Any]], op_id: str, *, replayed: bool
) -> PlanReplanApplied:
    return PlanReplanApplied(
        plan=plan_out(plan),
        results=[PlanReplanResult(**r) for r in results],
        operation_id=op_id,
        replayed=replayed,
    )


def _replay_replan(ctx: ApiContext, plan_id: str, op: Operation) -> PlanReplanApplied:
    """An earlier approval under the same key: its results and the plan as
    it is now, or the refusal it recorded, or ``409`` while it still runs."""
    replayed_refusal(op)
    try:
        plan = ctx.plans.get(plan_id)
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    return _replan_applied(plan, list((op.result or {}).get("results") or []), op.id, replayed=True)


@router.post(
    "/{plan_id}/nodes/{node_id}/replan/approve",
    response_model=PlanReplanApplied,
    summary="Apply a re-plan's diff to the forge",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 503: PROBLEM},
)
async def approve_replan(
    plan_id: str,
    node_id: str,
    body: PlanReplanApprove,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> PlanReplanApplied:
    """Apply the node's waiting re-plan — every entry, or those
    ``entry_ids`` names — through the publish path: an ``add`` becomes an
    approved child published with its marker, level label and link (looked
    for by its marker first, never filed twice); a ``modify`` rewrites the
    child's title and the sections under the rendered headings that change,
    refused when the child or its issue moved since; a ``suggest_close``
    closes the issue as not planned and comments why. An entry that lands
    leaves the diff; one that fails stays with its error. Refused before
    the forge is touched: no re-plan waiting (``409 no_replan``), an entry
    not in it (``422``), a disabled repository, a full level (``409
    too_many_children``), an addition depending on one not approved with it
    (``409 dependency_unpublished``), no forge connection (``503``). The
    ``Idempotency-Key`` header is required: a replay answers the same
    results."""
    principal = auth.principal
    pair = idempotency(
        request,
        principal,
        f"/v1/plans/{plan_id}/nodes/{node_id}/replan/approve",
        required=True,
    )
    actor = actor_of(auth)

    def run() -> PlanReplanApplied:
        return recorded(
            ctx,
            OperationSpec(
                action=REPLAN_ACTION,
                target_kind="plan",
                target_key=plan_id,
                principal=principal,
                request={
                    "plan_id": plan_id,
                    "node_id": node_id,
                    "expected_revision": body.expected_revision,
                    "entry_ids": body.entry_ids,
                },
                idempotency=pair,
                expected_revision=body.expected_revision,
            ),
            replay=lambda op: _replay_replan(ctx, plan_id, op),
            call=lambda: ctx.plans.approve_replan(
                plan_id,
                node_id,
                expected_revision=body.expected_revision,
                entry_ids=body.entry_ids,
                actor=actor,
                **_forge_args(ctx),
            ),
            result=lambda applied: {
                "results": [r.as_dict() for r in applied.results],
                "revision": applied.plan.revision,
            },
            out=lambda applied, op_id: _replan_applied(
                applied.plan, [r.as_dict() for r in applied.results], op_id, replayed=False
            ),
        )

    applied = await ctx.call(run)
    ctx.hub.notify()
    return applied


@router.post(
    "/{plan_id}/nodes/{node_id}/replan/discard",
    response_model=PlanOut,
    summary="Discard a re-plan's diff",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM},
)
async def discard_replan(
    plan_id: str,
    node_id: str,
    body: PlanReplanDiscard,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Drop the node's waiting re-plan — every entry, or those
    ``entry_ids`` names — without writing anything to the forge. No
    re-plan waiting is ``409 no_replan``; an entry not in it is ``422``."""
    try:
        plan = await ctx.call(
            ctx.plans.discard_replan,
            plan_id,
            node_id,
            expected_revision=body.expected_revision,
            entry_ids=body.entry_ids,
            now=ctx.clock(),
            actor=actor_of(auth),
        )
    except PlanRefusal as exc:
        raise problem_of(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


# -- epic runs (#2347) -------------------------------------------------------------
