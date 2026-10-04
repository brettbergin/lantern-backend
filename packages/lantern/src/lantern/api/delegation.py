"""Grants and the decisions ledger, as the API shows and writes them.

A grant is written through the shared service, under ``policy:manage``,
as one recorded operation each — the same shape a schedule's writes have
(:mod:`lantern.api.admin`). The reads are projections of the daemon's
delegation store: a grant with how many acts it allowed today, and a
decision with its target named by public ids.

No command frame on the WebSocket reaches these, and no chat tool: the
standing rules that let agents take decisions are edited here and nowhere
else.
"""

from __future__ import annotations

from typing import Any

from lantern.api.admin import _apply, _outcome_message
from lantern.api.auth.deps import Authenticated
from lantern.api.context import ApiContext
from lantern.api.models import (
    DecisionOut,
    GrantConditions,
    GrantCreate,
    GrantOut,
    GrantResult,
    GrantsRestored,
    GrantUpdate,
    OperationOut,
    rfc3339,
)
from lantern.api.projections import not_found
from lantern.api.publicids import run_public_id
from lantern.daemon.controls.delegation import Grant
from lantern.daemon.controls.delegation_defaults import default_order
from lantern.daemon.controls.delegation_store import DecisionRecord, DelegationStore
from lantern.daemon.controls.operations import Operation
from lantern.daemon.controls.results import Outcome
from lantern.ghids import normalize_item_id


def store_of(ctx: ApiContext) -> DelegationStore:
    """The daemon's one delegation store."""
    store: DelegationStore = ctx.loop.delegation
    return store


def _used_today(ctx: ApiContext) -> dict[str, int]:
    """What each grant allowed in the current cap day: the day the usage
    pool's run cap counts in."""
    day_start, _next = ctx.loop.usage_pool.day(ctx.clock())
    return store_of(ctx).used_today(day_start)


def grant_out(grant: Grant, used: int = 0) -> GrantOut:
    held = grant.conditions
    return GrantOut(
        id=grant.id,
        agent_slug=grant.agent_slug,
        action=grant.action,
        conditions=GrantConditions(
            repositories=None if held.repositories is None else list(held.repositories),
            levels=None if held.levels is None else list(held.levels),
            max_children=held.max_children,
            require_review=held.require_review,
            causes=None if held.causes is None else list(held.causes),
            max_retries=held.max_retries,
        ),
        daily_limit=grant.daily_limit,
        used_today=used,
        enabled=grant.enabled,
        note=grant.note,
        created_by=grant.created_by,
        created_by_display=grant.created_by_display,
        created_at=rfc3339(grant.created_at) or "",
        updated_at=rfc3339(grant.updated_at) or "",
        revision=grant.revision,
        source=grant.source,
        default_key=grant.default_key,
    )


def listed_order(grants: list[Grant]) -> list[Grant]:
    """Lantern's defaults first, in the table's order, then the owner's
    grants oldest first. The judge does not read this order: it picks the
    oldest grant that allows an act."""
    defaults = sorted(
        (grant for grant in grants if grant.source == "default"),
        key=lambda grant: (default_order(grant.default_key), grant.created_at, grant.id),
    )
    owners = [grant for grant in grants if grant.source != "default"]
    return defaults + owners


def list_grants(ctx: ApiContext) -> list[GrantOut]:
    """Lantern's defaults in the table's order, then the owner's grants
    oldest first."""
    used = _used_today(ctx)
    return [
        grant_out(grant, used.get(grant.id, 0)) for grant in listed_order(store_of(ctx).grants())
    ]


def get_grant(ctx: ApiContext, grant_id: str) -> GrantOut:
    grant = store_of(ctx).grant(grant_id)
    if grant is None:
        raise not_found()
    return grant_out(grant, _used_today(ctx).get(grant.id, 0))


def decision_out(ctx: ApiContext, row: DecisionRecord) -> DecisionOut:
    """A ledger row with its run and item named by their public ids. An
    item's key is its repository and its id together, which is why the
    ledger keeps the repository beside the item."""
    item_id: str | None = None
    if row.item_id:
        key = f"{row.repository or ''}|{normalize_item_id(row.item_id)}"
        item_id = ctx.public_ids.assign("item", [key], ctx.clock())[key]
    return DecisionOut(
        id=row.id,
        grant_id=row.grant_id,
        agent_slug=row.agent_slug,
        action=row.action,
        outcome=row.outcome,
        reason=row.reason,
        plan_id=row.plan_id,
        node_id=row.node_id,
        item_id=item_id,
        run_id=run_public_id(row.run_id) if row.run_id else None,
        epic_run_id=row.epic_run_id,
        repository=row.repository,
        operation_id=row.operation_id,
        attrs=dict(row.attrs),
        at=rfc3339(row.at) or "",
        resolved_at=rfc3339(row.resolved_at),
        resolved_by=row.resolved_by,
        resolution=row.resolution,
    )


def _result(ctx: ApiContext, grant_id: str, message: str, operation: Operation) -> GrantResult:
    grant = store_of(ctx).grant(grant_id)
    return GrantResult(
        grant=None if grant is None else grant_out(grant, _used_today(ctx).get(grant.id, 0)),
        message=message,
        operation=OperationOut.from_operation(operation),
    )


async def create_grant(
    ctx: ApiContext, auth: Authenticated, body: GrantCreate, pair: tuple[str, str] | None
) -> GrantResult:
    """Write a grant. The service refuses, naming the field, an action
    outside the closed list, a condition the action does not accept, an
    agent the registry does not know or that is disabled, and a daily
    limit that is not positive."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        return service.add_grant(
            principal,
            agent_slug=body.agent_slug,
            action=body.action,
            conditions=body.conditions.model_dump(),
            daily_limit=body.daily_limit,
            enabled=body.enabled,
            note=body.note,
            idempotency=pair,
        )

    outcome, operation = await _apply(ctx, apply)
    # A replay answers with the grant the first attempt wrote: the
    # operation's target is the grant.
    result = await ctx.call(
        _result, ctx, operation.target_key, _outcome_message(outcome, operation), operation
    )
    ctx.hub.notify()
    return result


async def update_grant(
    ctx: ApiContext,
    auth: Authenticated,
    grant_id: str,
    body: GrantUpdate,
    pair: tuple[str, str] | None,
) -> GrantResult:
    """Edit a grant against the revision the client read; only the fields
    sent change."""
    principal = auth.principal
    service = ctx.service()
    changes: dict[str, Any] = {
        field: getattr(body, field)
        for field in body.model_fields_set
        if field != "expected_revision"
    }
    if "conditions" in changes:
        sent = body.conditions
        changes["conditions"] = {} if sent is None else sent.model_dump()

    def apply() -> Outcome:
        if store_of(ctx).grant(grant_id) is None:
            raise not_found()
        return service.update_grant(
            principal,
            grant_id,
            changes,
            expected_revision=body.expected_revision,
            idempotency=pair,
        )

    outcome, operation = await _apply(ctx, apply)
    result = await ctx.call(_result, ctx, grant_id, _outcome_message(outcome, operation), operation)
    ctx.hub.notify()
    return result


async def remove_grant(
    ctx: ApiContext, auth: Authenticated, grant_id: str, pair: tuple[str, str] | None
) -> GrantResult:
    """Delete a grant; what it allowed stays in the ledger."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        if store_of(ctx).grant(grant_id) is None:
            raise not_found()
        return service.remove_grant(principal, grant_id, idempotency=pair)

    outcome, operation = await _apply(ctx, apply)
    result = GrantResult(
        grant=None,
        message=_outcome_message(outcome, operation),
        operation=OperationOut.from_operation(operation),
    )
    ctx.hub.notify()
    return result


async def restore_defaults(
    ctx: ApiContext, auth: Authenticated, pair: tuple[str, str] | None
) -> GrantsRestored:
    """Write again each default grant that is gone; one still there,
    edited or paused, is left as it is."""
    principal = auth.principal
    service = ctx.service()

    def apply() -> Outcome:
        return service.restore_default_grants(principal, idempotency=pair)

    outcome, operation = await _apply(ctx, apply)
    # A replay answers with the grants the first attempt wrote, as they
    # are now (one deleted since is left out).
    ids = list(
        getattr(outcome, "grant_ids", None) or (operation.result or {}).get("grant_ids") or []
    )

    def read() -> GrantsRestored:
        used = _used_today(ctx)
        store = store_of(ctx)
        grants = [
            grant for grant in (store.grant(grant_id) for grant_id in ids) if grant is not None
        ]
        return GrantsRestored(
            grants=[grant_out(grant, used.get(grant.id, 0)) for grant in grants],
            message=_outcome_message(outcome, operation),
            operation=OperationOut.from_operation(operation),
        )

    result = await ctx.call(read)
    ctx.hub.notify()
    return result
