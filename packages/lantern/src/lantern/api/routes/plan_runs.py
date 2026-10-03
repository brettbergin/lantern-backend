"""Epic runs (#2347, #2348): the one path from plan content to queued work,
and a person's controls on it.

``POST /v1/plans/{id}/nodes/{epic_id}/run`` starts a run on a published
epic whose tasks are on the forge; the daemon then admits each task whose
dependencies are closed as an issue run with ``parent_item_id`` naming the
run. ``GET`` reads the epic's latest run; ``pause``, ``resume``,
``cancel``, ``retry`` and ``skip`` steer it. Every write here is a
recorded operation under an ``Idempotency-Key`` (see
:func:`lantern.api.routes.plans.recorded`). Feature ``planning``;
``plans:publish`` to start or steer, ``runs:read`` to read.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, Response

from lantern.api.auth.deps import Authenticated, get_ctx, require
from lantern.api.commands import idempotency
from lantern.api.context import ApiContext
from lantern.api.errors import Problem
from lantern.api.models import rfc3339
from lantern.api.plan_schemas import (
    EpicRunChanged,
    EpicRunOut,
    EpicRunStart,
    EpicRunStarted,
    EpicRunTaskOut,
    PlanForge,
)
from lantern.api.routes.plans import (
    PROBLEM,
    actor_of,
    problem_of,
    recorded,
    replayed_refusal,
)
from lantern.daemon.controls.operations import Operation, OperationSpec
from lantern.plans import Plan, PlanRefusal
from lantern.plans.epicrun import EpicRun

router = APIRouter(prefix="/v1/plans", tags=["plans"])

RUN_ACTION = "plan.run"


def epic_run_out(run: EpicRun, plan: Plan | None) -> dict[str, Any]:
    """An epic run's fields, each task named from the plan as it is now."""
    tasks: list[EpicRunTaskOut] = []
    for task in run.tasks:
        node = plan.node(task.node_id) if plan is not None else None
        tasks.append(
            EpicRunTaskOut(
                node_id=task.node_id,
                title=node.title if node is not None else task.node_id,
                kind=node.kind if node is not None else None,
                workload_profile=node.workload_profile if node is not None else None,
                depends_on=list(node.depends_on) if node is not None else [],
                forge=(
                    None
                    if node is None or node.forge is None
                    else PlanForge(
                        number=node.forge.number, url=node.forge.url, state=node.forge.state
                    )
                ),
                state=task.state,
                item_id=task.item_id,
                run_id=task.run_id,
                reason=task.reason,
                admitted_at=rfc3339(task.admitted_at),
                updated_at=rfc3339(task.updated_at) or "",
            )
        )
    return {
        "id": run.id,
        "plan_id": run.plan_id,
        "node_id": run.node_id,
        "state": run.state,
        "started_by": run.started_by,
        "started_by_display": run.started_by_display,
        "created_at": rfc3339(run.created_at) or "",
        "updated_at": rfc3339(run.updated_at) or "",
        "completed_at": rfc3339(run.completed_at),
        "tasks": tasks,
    }


def _driver(ctx: ApiContext) -> Any:
    driver = getattr(ctx.loop, "epic_runs", None)
    if driver is None:
        raise Problem(503, "daemon_not_ready", "this daemon runs no epic runs")
    return driver


@router.get(
    "/{plan_id}/nodes/{node_id}/run",
    response_model=EpicRunOut,
    summary="Read an epic's run",
    responses={404: PROBLEM},
)
async def get_epic_run(
    plan_id: str,
    node_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> EpicRunOut:
    """The epic's most recent run, each task's state and the item and run
    it became, so a client links each task to its run's thread."""

    def read() -> EpicRunOut:
        plan = ctx.plans.get(plan_id)
        run = _driver(ctx).latest(plan_id, node_id)
        return EpicRunOut(**epic_run_out(run, plan))

    try:
        return await ctx.call(read)
    except PlanRefusal as exc:
        raise problem_of(exc) from exc


@router.post(
    "/{plan_id}/nodes/{node_id}/run",
    response_model=EpicRunStarted,
    status_code=201,
    summary="Run an epic",
    responses={404: PROBLEM, 409: PROBLEM, 422: PROBLEM, 503: PROBLEM},
)
async def run_epic(
    plan_id: str,
    node_id: str,
    body: EpicRunStart,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> EpicRunStarted:
    """Start an epic run on a published epic whose tasks are on the forge.
    The daemon owns it from here: every task whose dependencies are closed
    is admitted through the same issue admission as ``POST /v1/items``
    (never by the trigger label) with ``parent_item_id`` naming the run — a
    code task as a code run, a workload task as a workload run under its
    profile — and each one that lands or delivers makes its dependents
    ready. A failed task's dependents are not admitted. The
    ``Idempotency-Key`` header is required: a replay answers the run as it
    is now; a different body under the same key is ``409
    idempotency_conflict``."""
    principal = auth.principal
    pair = idempotency(
        request, principal, f"/v1/plans/{plan_id}/nodes/{node_id}/run", required=True
    )
    actor = actor_of(auth)

    def replay(op: Operation) -> EpicRunStarted:
        replayed_refusal(op)
        driver = _driver(ctx)
        run = driver.runs.get(str((op.result or {}).get("epic_run_id") or ""))
        if run is None:
            run = driver.latest(plan_id, node_id)
        response.status_code = 200
        return EpicRunStarted(
            **epic_run_out(run, ctx.plans.store.get(plan_id)), operation_id=op.id, replayed=True
        )

    def start() -> EpicRunStarted:
        driver = _driver(ctx)
        return recorded(
            ctx,
            OperationSpec(
                action=RUN_ACTION,
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
            replay=replay,
            call=lambda: driver.start(
                plan_id,
                node_id,
                expected_revision=body.expected_revision,
                actor=actor,
                now=ctx.clock(),
            ),
            result=lambda run: {"epic_run_id": run.id},
            out=lambda run, op_id: EpicRunStarted(
                **epic_run_out(run, ctx.plans.store.get(plan_id)),
                operation_id=op_id,
                replayed=False,
            ),
        )

    started = await ctx.call(start)
    if not started.replayed:
        response.headers["Location"] = f"/v1/plans/{plan_id}/nodes/{node_id}/run"
    ctx.hub.notify()
    return started


# -- epic run controls (#2348) ----------------------------------------------------

ControlVerb = Literal["pause", "resume", "cancel", "retry", "skip"]


async def control_epic_run(
    ctx: ApiContext,
    auth: Authenticated,
    verb: ControlVerb,
    plan_id: str,
    node_id: str,
    pair: tuple[str, str] | None,
) -> EpicRunChanged:
    """One control on an epic run, recorded as a ``plan.run.<verb>``
    operation under the caller's idempotency ``pair``: a replay answers
    the run as it is now (or the refusal it recorded), and a different
    request under the same key is ``409 idempotency_conflict``. The
    command behind each control route here, and behind an act on an
    ``epic_task`` entry of ``/v1/attention``."""
    principal = auth.principal
    actor = actor_of(auth)

    def replay(op: Operation) -> EpicRunChanged:
        replayed_refusal(op)
        driver = _driver(ctx)
        run = driver.runs.get(str((op.result or {}).get("epic_run_id") or ""))
        if run is None:
            run = driver.run_for(plan_id, node_id)
        return EpicRunChanged(
            **epic_run_out(run, ctx.plans.store.get(plan_id)), operation_id=op.id, replayed=True
        )

    def apply() -> EpicRunChanged:
        driver = _driver(ctx)
        control: Callable[..., EpicRun] = getattr(driver, verb)
        return recorded(
            ctx,
            OperationSpec(
                action=f"{RUN_ACTION}.{verb}",
                target_kind="plan",
                target_key=plan_id,
                principal=principal,
                request={"plan_id": plan_id, "node_id": node_id},
                idempotency=pair,
            ),
            replay=replay,
            call=lambda: control(plan_id, node_id, actor=actor, now=ctx.clock()),
            result=lambda run: {"epic_run_id": run.id},
            out=lambda run, op_id: EpicRunChanged(
                **epic_run_out(run, ctx.plans.store.get(plan_id)),
                operation_id=op_id,
                replayed=False,
            ),
        )

    changed = await ctx.call(apply)
    ctx.hub.notify()
    return changed


async def _control(
    verb: ControlVerb,
    plan_id: str,
    node_id: str,
    request: Request,
    ctx: ApiContext,
    auth: Authenticated,
) -> EpicRunChanged:
    """A control route's answer: the ``Idempotency-Key`` header is
    required and scoped to the route."""
    pair = idempotency(
        request, auth.principal, f"/v1/plans/{plan_id}/nodes/{node_id}/run/{verb}", required=True
    )
    return await control_epic_run(ctx, auth, verb, plan_id, node_id, pair)


_CONTROL_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: PROBLEM,
    409: PROBLEM,
    422: PROBLEM,
    503: PROBLEM,
}


@router.post(
    "/{plan_id}/nodes/{node_id}/run/pause",
    response_model=EpicRunChanged,
    summary="Pause an epic run",
    responses=_CONTROL_RESPONSES,
)
async def pause_epic_run(
    plan_id: str,
    node_id: str,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> EpicRunChanged:
    """Stop the epic's running run from admitting anything new. Tasks
    already queued or running are not cancelled: they go on, and the run
    still follows them. Refused when the run is already paused (``409
    already_paused``) or has ended (``409 run_ended``). Records
    ``plan.run.paused`` with ``reason: "person"``."""
    return await _control("pause", plan_id, node_id, request, ctx, auth)


@router.post(
    "/{plan_id}/nodes/{node_id}/run/resume",
    response_model=EpicRunChanged,
    summary="Resume an epic run",
    responses=_CONTROL_RESPONSES,
)
async def resume_epic_run(
    plan_id: str,
    node_id: str,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> EpicRunChanged:
    """Resume a paused epic run: the pass made here admits every ready
    task at once. Refused when the run is not paused (``409 not_paused``)
    or has ended (``409 run_ended``). Records ``plan.run.resumed``."""
    return await _control("resume", plan_id, node_id, request, ctx, auth)


@router.post(
    "/{plan_id}/nodes/{node_id}/run/cancel",
    response_model=EpicRunChanged,
    summary="Stop an epic run",
    responses=_CONTROL_RESPONSES,
)
async def cancel_epic_run(
    plan_id: str,
    node_id: str,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> EpicRunChanged:
    """Stop the epic run for good: it is ``cancelled``. A task not yet
    admitted never is (``cancelled``); a task whose item is still waiting
    in the queue has the item withdrawn through the item abandon
    (``cancelled``); a task whose run is under way is not killed — it
    finishes, the run follows it, and a person cancels that run through
    ``POST /v1/runs/{run_id}/cancel`` if they want it down. Refused when
    the run has ended (``409 run_ended``). Records ``plan.run.cancelled``
    with the task node ids ``withdrawn`` and still ``running``."""
    return await _control("cancel", plan_id, node_id, request, ctx, auth)


@router.post(
    "/{plan_id}/nodes/{node_id}/run/retry",
    response_model=EpicRunChanged,
    summary="Retry a failed task of an epic run",
    responses=_CONTROL_RESPONSES,
)
async def retry_epic_run_task(
    plan_id: str,
    node_id: str,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> EpicRunChanged:
    """Run a failed task again, in the latest epic run that holds it. An
    item that failed, was blocked or was cancelled is re-queued through the
    item retry (attempts start over, a fresh run; the issue hears who
    asked); a task whose admission was refused is admitted afresh. Its
    dependents wait on it again. Allowed while the run is paused. Refused
    when the task is blocked by another (``409 task_blocked`` with
    ``blocked_by``), is not failed (``409 task_not_failed``), or the run
    has ended (``409 run_ended``). Records ``plan.run.task_retried``."""
    return await _control("retry", plan_id, node_id, request, ctx, auth)


@router.post(
    "/{plan_id}/nodes/{node_id}/run/skip",
    response_model=EpicRunChanged,
    summary="Skip a task of an epic run",
    responses=_CONTROL_RESPONSES,
)
async def skip_epic_run_task(
    plan_id: str,
    node_id: str,
    request: Request,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> EpicRunChanged:
    """Treat a task that is not under way — failed, blocked, waiting or
    ready — as done, so its dependents become ready. The task is
    ``skipped``; its issue is left exactly as it is (lantern does not
    close it) and its item is not touched. Refused when the task is queued
    or running (``409 task_in_progress``), already settled (``409
    task_settled``) or the run has ended (``409 run_ended``). Records
    ``plan.run.task_skipped``."""
    return await _control("skip", plan_id, node_id, request, ctx, auth)
