"""Goals, as the API shows and writes them.

A goal is written through the shared service, under ``plans:publish``, as
one recorded operation each — the shape a grant's writes have
(:mod:`lantern.api.delegation`). A read is the daemon's goal with the
plans proposed from it, read from the plans that name it in one query.

No command frame on the WebSocket reaches these, and no chat tool.
"""

from __future__ import annotations

from typing import Any, cast

from lantern.api.admin import _apply, _outcome_message
from lantern.api.auth.deps import Authenticated
from lantern.api.context import ApiContext
from lantern.api.models import (
    GoalCreate,
    GoalOut,
    GoalPlanOut,
    GoalProposingOut,
    GoalResult,
    GoalUpdate,
    OperationOut,
    rfc3339,
)
from lantern.api.projections import not_found
from lantern.config import Config
from lantern.daemon.controls.operations import Operation
from lantern.daemon.controls.results import Outcome
from lantern.daemon.goals import Goal, GoalPlan, GoalState, GoalStore, open_plan


def store_of(ctx: ApiContext) -> GoalStore:
    """The daemon's one goal store."""
    store: GoalStore = ctx.loop.goals
    return store


#: Why the planner does not propose toward any goal on this server.
PROPOSING_OFF = (
    "proposing is off on this server: an operator sets [delegation] propose_every "
    "to let the planner draft plans toward goals"
)


def proposing_out(config: Config, goal: Goal) -> GoalProposingOut:
    """Whether the planner drafts plans toward ``goal`` on its own: only
    while ``[delegation] propose_every`` is set and the goal is active."""
    every = int(config.delegation.propose_every)
    if every <= 0:
        return GoalProposingOut(enabled=False, every_s=0, reason=PROPOSING_OFF)
    if goal.state != "active":
        return GoalProposingOut(
            enabled=False,
            every_s=every,
            reason=f"the goal is {goal.state}; the planner proposes only toward active goals",
        )
    return GoalProposingOut(enabled=True, every_s=every)


def goal_out(goal: Goal, plans: list[GoalPlan], config: Config) -> GoalOut:
    serving = open_plan(plans)
    return GoalOut(
        id=goal.id,
        repository=goal.repository,
        title=goal.title,
        text=goal.text,
        state=goal.state,
        created_by=goal.created_by,
        created_by_display=goal.created_by_display,
        created_at=rfc3339(goal.created_at) or "",
        updated_at=rfc3339(goal.updated_at) or "",
        revision=goal.revision,
        plans=[
            GoalPlanOut(
                plan_id=plan.plan_id,
                title=plan.title,
                state=plan.state,
                advance=cast(Any, plan.advance),
            )
            for plan in plans
        ],
        open_plan_id=None if serving is None else serving.plan_id,
        proposing=proposing_out(config, goal),
    )


def list_goals(
    ctx: ApiContext, *, repository: str | None = None, state: GoalState | None = None
) -> list[GoalOut]:
    """Every goal, oldest first, each with the plans proposed from it."""
    store = store_of(ctx)
    goals = store.goals(repository=repository, state=state)
    served = store.plans_by_goal(goal.id for goal in goals)
    return [goal_out(goal, served.get(goal.id, []), ctx.config) for goal in goals]


def get_goal(ctx: ApiContext, goal_id: str) -> GoalOut:
    store = store_of(ctx)
    goal = store.goal(goal_id)
    if goal is None:
        raise not_found()
    return goal_out(goal, store.plans_for(goal.id), ctx.config)


def _result(ctx: ApiContext, goal_id: str, message: str, operation: Operation) -> GoalResult:
    store = store_of(ctx)
    goal = store.goal(goal_id)
    return GoalResult(
        goal=None if goal is None else goal_out(goal, store.plans_for(goal.id), ctx.config),
        message=message,
        operation=OperationOut.from_operation(operation),
    )


async def create_goal(
    ctx: ApiContext, auth: Authenticated, body: GoalCreate, pair: tuple[str, str] | None
) -> GoalResult:
    """Write a goal. The service refuses, naming the field, a repository
    that is not configured, is disabled or cannot hold a plan."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        return service.add_goal(
            principal,
            repository=body.repository,
            title=body.title,
            text=body.text,
            state=body.state,
            idempotency=pair,
        )

    outcome, operation = await _apply(ctx, apply)
    # A replay answers with the goal the first attempt wrote: the
    # operation's target is the goal.
    result = await ctx.call(
        _result, ctx, operation.target_key, _outcome_message(outcome, operation), operation
    )
    ctx.hub.notify()
    return result


async def update_goal(
    ctx: ApiContext,
    auth: Authenticated,
    goal_id: str,
    body: GoalUpdate,
    pair: tuple[str, str] | None,
) -> GoalResult:
    """Edit a goal against the revision the client read; only the fields
    sent change."""
    principal = auth.principal
    service = ctx.service()
    changes: dict[str, Any] = {
        field: getattr(body, field)
        for field in body.model_fields_set
        if field != "expected_revision"
    }

    def apply() -> Outcome:
        if store_of(ctx).goal(goal_id) is None:
            raise not_found()
        return service.update_goal(
            principal,
            goal_id,
            changes,
            expected_revision=body.expected_revision,
            idempotency=pair,
        )

    outcome, operation = await _apply(ctx, apply)
    result = await ctx.call(_result, ctx, goal_id, _outcome_message(outcome, operation), operation)
    ctx.hub.notify()
    return result


async def remove_goal(
    ctx: ApiContext, auth: Authenticated, goal_id: str, pair: tuple[str, str] | None
) -> GoalResult:
    """Delete a goal; the plans proposed from it keep naming it."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        if store_of(ctx).goal(goal_id) is None:
            raise not_found()
        return service.remove_goal(principal, goal_id, idempotency=pair)

    outcome, operation = await _apply(ctx, apply)
    result = GoalResult(
        goal=None,
        message=_outcome_message(outcome, operation),
        operation=OperationOut.from_operation(operation),
    )
    ctx.hub.notify()
    return result
