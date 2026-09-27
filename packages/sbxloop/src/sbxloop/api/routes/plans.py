"""Plans: initiatives, epics and tasks, drafted here and published to the
forge one level at a time (#2334).

Feature ``planning``. Every plan, drafts included, is readable by anyone
holding ``runs:read``; drafting and editing need ``plans:create``. Every
mutation names the ``expected_revision`` it read; a stale one is ``409
stale_revision`` with the plan's current revision.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Response

from sbxloop.api.auth.deps import Authenticated, get_ctx, require
from sbxloop.api.context import ApiContext
from sbxloop.api.errors import Problem
from sbxloop.api.models import rfc3339
from sbxloop.api.pagination import Page
from sbxloop.api.plan_schemas import (
    PlanCreate,
    PlanDeleted,
    PlanForge,
    PlanNodeCreate,
    PlanNodeOut,
    PlanNodeUpdate,
    PlanOut,
    PlanRollup,
    PlanSummary,
    PlanUpdate,
)
from sbxloop.plans import Plan, PlanNode, PlanRefusal
from sbxloop.plans.service import SECTIONS

router = APIRouter(prefix="/v1/plans", tags=["plans"])

_PROBLEM = {"description": "A refusal, as `application/problem+json`."}


def _problem(exc: PlanRefusal) -> Problem:
    return Problem(exc.status, exc.code, exc.detail, **exc.extra)


def _actor(auth: Authenticated) -> dict[str, Any]:
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
            else PlanForge(number=node.forge.number, url=node.forge.url, state=node.forge.state)
        ),
        created_at=rfc3339(node.created_at) or "",
        updated_at=rfc3339(node.updated_at) or "",
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
        "title": root.title,
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
    }


def plan_out(plan: Plan) -> PlanOut:
    return PlanOut(**_summary_fields(plan), nodes=[node_out(n) for n in plan.nodes])


@router.get("", response_model=Page[PlanSummary], summary="List plans")
async def list_plans(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    repository: Annotated[str | None, Query(max_length=200)] = None,
    level: Annotated[Literal["initiative", "epic"] | None, Query()] = None,
    state: Annotated[Literal["draft", "published", "archived"] | None, Query()] = None,
) -> Page[PlanSummary]:
    """Every plan in the workspace, drafts included, most recently changed
    first. ``repository`` matches a plan any of whose nodes targets it."""
    plans = await ctx.call(ctx.plans.find, repository=repository, level=level, state=state)
    return Page(data=[PlanSummary(**_summary_fields(plan)) for plan in plans])


@router.post(
    "",
    response_model=PlanOut,
    status_code=201,
    summary="Draft a plan",
    responses={409: _PROBLEM, 422: _PROBLEM},
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
            actor=_actor(auth),
        )
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.get("/{plan_id}", response_model=PlanOut, summary="Read a plan", responses={404: _PROBLEM})
async def get_plan(
    plan_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> PlanOut:
    try:
        plan = await ctx.call(ctx.plans.get, plan_id)
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    return plan_out(plan)


@router.patch(
    "/{plan_id}",
    response_model=PlanOut,
    summary="Edit a plan",
    responses={404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
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
            actor=_actor(auth),
        )
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.delete(
    "/{plan_id}",
    response_model=PlanDeleted,
    summary="Delete a draft plan or archive a published one",
    responses={404: _PROBLEM, 409: _PROBLEM},
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
            actor=_actor(auth),
        )
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return PlanDeleted(id=plan_id, outcome=outcome)  # type: ignore[arg-type]


@router.post(
    "/{plan_id}/nodes",
    response_model=PlanOut,
    status_code=201,
    summary="Add a node to a plan",
    responses={404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def add_node(
    plan_id: str,
    body: PlanNodeCreate,
    response: Response,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """A new child one level under ``parent_id``; the plan as it now is,
    with the new node's id in the ``Location`` header."""
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
            actor=_actor(auth),
        )
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    response.headers["Location"] = f"/v1/plans/{plan_id}/nodes/{node_id}"
    ctx.hub.notify()
    return plan_out(plan)


@router.patch(
    "/{plan_id}/nodes/{node_id}",
    response_model=PlanOut,
    summary="Edit or move a node",
    responses={404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def update_node(
    plan_id: str,
    node_id: str,
    body: PlanNodeUpdate,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Edit a node's sections or move it among its siblings. Editing a
    proposed or approved node makes it a draft again; a published node is
    ``409 node_published``."""
    try:
        plan = await ctx.call(
            ctx.plans.update_node,
            plan_id,
            node_id,
            expected_revision=body.expected_revision,
            sections=_sections(body),
            position=body.position,
            now=ctx.clock(),
            actor=_actor(auth),
        )
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)


@router.delete(
    "/{plan_id}/nodes/{node_id}",
    response_model=PlanOut,
    summary="Remove a node",
    responses={404: _PROBLEM, 409: _PROBLEM, 422: _PROBLEM},
)
async def remove_node(
    plan_id: str,
    node_id: str,
    expected_revision: Annotated[int, Query(ge=1)],
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:create")),  # noqa: B008
) -> PlanOut:
    """Remove an unpublished node and everything under it."""
    try:
        plan = await ctx.call(
            ctx.plans.remove_node,
            plan_id,
            node_id,
            expected_revision=expected_revision,
            now=ctx.clock(),
            actor=_actor(auth),
        )
    except PlanRefusal as exc:
        raise _problem(exc) from exc
    ctx.hub.notify()
    return plan_out(plan)
