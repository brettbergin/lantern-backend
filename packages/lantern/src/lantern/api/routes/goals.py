"""Goals: the standing objectives an owner writes for a repository, and the
plans proposed from each.

Reading takes ``runs:read``. Writing takes ``plans:publish``: direction is
set by an admin or an owner, not by a member.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from lantern.api import goals
from lantern.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from lantern.api.commands import idempotency
from lantern.api.context import ApiContext
from lantern.api.models import GoalCreate, GoalOut, GoalResult, GoalStateName, GoalUpdate
from lantern.api.pagination import Page

router = APIRouter(prefix="/v1", tags=["goals"])


@router.get("/goals", response_model=Page[GoalOut])
async def list_goals(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    repository: Annotated[str | None, Query(max_length=200)] = None,
    state: Annotated[GoalStateName | None, Query()] = None,
) -> Page[GoalOut]:
    """Every goal, oldest first, narrowed by `repository` and `state`
    (`active`, `paused`, `done`). Each carries the plans proposed from it
    and `open_plan_id`, the one currently serving it."""
    return Page(data=await ctx.call(goals.list_goals, ctx, repository=repository, state=state))


@router.get("/goals/{goal_id}", response_model=GoalOut)
async def get_goal(
    goal_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> GoalOut:
    return await ctx.call(goals.get_goal, ctx, goal_id)


@router.post("/goals", response_model=GoalResult, status_code=201)
async def create_goal(
    body: GoalCreate,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> GoalResult:
    """Set a goal for a repository. `422` names the field: a repository
    that is not configured, is disabled or cannot hold a plan."""
    pair = idempotency(request, auth.principal, "/v1/goals", required=False)
    return await goals.create_goal(ctx, auth, body, pair)


@router.patch("/goals/{goal_id}", response_model=GoalResult)
async def update_goal(
    goal_id: str,
    body: GoalUpdate,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> GoalResult:
    """Edit a goal's title, text or state against `expected_revision`
    (`409 stale_revision` when it moved on). Its repository is not
    edited."""
    pair = idempotency(request, auth.principal, f"/v1/goals/{goal_id}", required=False)
    return await goals.update_goal(ctx, auth, goal_id, body, pair)


@router.delete("/goals/{goal_id}", response_model=GoalResult)
async def remove_goal(
    goal_id: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("plans:publish")),  # noqa: B008
) -> GoalResult:
    """Delete a goal. The plans proposed from it keep naming it."""
    pair = idempotency(request, auth.principal, f"/v1/goals/{goal_id}", required=False)
    return await goals.remove_goal(ctx, auth, goal_id, pair)
