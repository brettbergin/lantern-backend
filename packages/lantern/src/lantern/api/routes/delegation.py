"""Delegation: the grants that let agents take decisions, and the ledger
of what was decided.

Reading either takes ``audit:read``. Writing a grant takes
``policy:manage`` — the capability, never a role: a plain API client that
reads as an owner because it holds ``daemon:manage`` does not hold it.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from lantern.api import delegation
from lantern.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from lantern.api.commands import idempotency
from lantern.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from lantern.api.errors import Problem
from lantern.api.models import DecisionOut, GrantCreate, GrantOut, GrantResult, GrantUpdate
from lantern.api.pagination import Page, decode_cursor, encode_cursor
from lantern.api.usage import parse_when
from lantern.daemon.controls.delegation import OUTCOMES
from lantern.daemon.controls.delegation_store import DecisionRecord

router = APIRouter(prefix="/v1", tags=["delegation"])


@router.get("/grants", response_model=Page[GrantOut])
async def list_grants(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("audit:read")),  # noqa: B008
) -> Page[GrantOut]:
    """Every grant, oldest first, with how many acts each allowed today.
    Empty until an owner writes one: a fresh installation delegates
    nothing."""
    return Page(data=await ctx.call(delegation.list_grants, ctx))


@router.get("/grants/{grant_id}", response_model=GrantOut)
async def get_grant(
    grant_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("audit:read")),  # noqa: B008
) -> GrantOut:
    return await ctx.call(delegation.get_grant, ctx, grant_id)


@router.post("/grants", response_model=GrantResult, status_code=201)
async def create_grant(
    body: GrantCreate,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("policy:manage")),  # noqa: B008
) -> GrantResult:
    """Let an agent take one of the delegable actions, under conditions.
    `422` names the field: an action that cannot be delegated, a condition
    the action does not accept, an agent that is unknown or disabled, a
    daily limit that is not positive."""
    pair = idempotency(request, auth.principal, "/v1/grants", required=False)
    return await delegation.create_grant(ctx, auth, body, pair)


@router.patch("/grants/{grant_id}", response_model=GrantResult)
async def update_grant(
    grant_id: str,
    body: GrantUpdate,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("policy:manage")),  # noqa: B008
) -> GrantResult:
    """Edit a grant's conditions, daily limit, note or whether it is
    enabled, against `expected_revision` (`409 stale_revision` when it
    moved on). Its agent and action are not edited."""
    pair = idempotency(request, auth.principal, f"/v1/grants/{grant_id}", required=False)
    return await delegation.update_grant(ctx, auth, grant_id, body, pair)


@router.delete("/grants/{grant_id}", response_model=GrantResult)
async def remove_grant(
    grant_id: str,
    request: Request,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("policy:manage")),  # noqa: B008
) -> GrantResult:
    """Delete a grant. The decisions it allowed stay in the ledger."""
    pair = idempotency(request, auth.principal, f"/v1/grants/{grant_id}", required=False)
    return await delegation.remove_grant(ctx, auth, grant_id, pair)


@router.get("/decisions", response_model=Page[DecisionOut])
async def list_decisions(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("audit:read")),  # noqa: B008
    outcome: Annotated[str | None, Query()] = None,
    unresolved: Annotated[bool, Query()] = False,
    agent: Annotated[str | None, Query(max_length=200)] = None,
    since: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[DecisionOut]:
    """What was decided for agents, newest first: filter by `outcome`
    (`allow`, `deny`, `escalate`), `unresolved=true` (escalations still
    waiting for a person), `agent` (a slug) and `since` (RFC 3339 or
    epoch)."""
    if outcome is not None and outcome not in OUTCOMES:
        raise Problem(422, "invalid_request", f"outcome must be one of {', '.join(OUTCOMES)}")
    try:
        since_at = None if since is None else parse_when(since, default=0.0)
    except ValueError as exc:
        raise Problem(422, "invalid_request", str(exc)) from exc
    filters: dict[str, Any] = {
        "outcome": outcome,
        "unresolved": unresolved,
        "agent": agent,
        "since": since_at,
    }
    after: tuple[float, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (float(key["a"]), str(key["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc

    def read() -> tuple[list[DecisionOut], DecisionRecord | None, bool]:
        rows = delegation.store_of(ctx).page(
            outcome=outcome,
            unresolved=unresolved,
            agent_slug=agent,
            since=since_at,
            after=after,
            limit=limit + 1,
        )
        more = len(rows) > limit
        rows = rows[:limit]
        last = rows[-1] if rows else None
        return [delegation.decision_out(ctx, row) for row in rows], last, more

    data, last, more = await ctx.call(read)
    next_cursor = (
        encode_cursor({"a": last.at, "i": last.id}, filters) if more and last is not None else None
    )
    return Page(data=data, next_cursor=next_cursor, has_more=more)
