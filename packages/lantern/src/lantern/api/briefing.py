"""The briefing (``GET /v1/briefing``): what happened while a person was
away, what needs them now, and whether work is lined up — one summary a
landing screen, a widget and a digest read in one request.

Nothing here is a second opinion. The outcomes are the runs the engine
store holds in an end state; the waiting counts are the attention list's
own computation (:func:`lantern.api.attention.waiting`); the decisions are
the delegation ledger's; the supply is counted in the plan store; the
budget is the usage pool's snapshot. Each is read in a bounded number of
statements — no loop over plans, runs or decisions — because this is
polled.

Two definitions a client should know. A run is **in the window** when it
*finished* in it: it rests in ``merged``, ``completed``, ``failed`` or
``cancelled`` and its last change falls inside the window. The analytics
fold keys a run on when it began; a briefing is about what ended while the
person was away, so a run asked for last week and landed this morning is
this morning's news. A task is **ready** (``supply.ready_tasks``) when it
is published, its issue is open and still followed, and no epic run has
started it — :class:`lantern.plans.store.Supply` spells it out.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from lantern.api.attention import _items_as_stored, counts, waiting
from lantern.api.models import (
    Briefing,
    BriefingBudget,
    BriefingDecided,
    BriefingDecision,
    BriefingGrants,
    BriefingLandedRun,
    BriefingLane,
    BriefingOutcomes,
    BriefingRunway,
    BriefingSupply,
    BriefingWaiting,
    rfc3339,
)
from lantern.api.projections import Views, item_repository
from lantern.api.publicids import run_public_id
from lantern.daemon.controls.delegation import OUTCOMES
from lantern.daemon.controls.delegation_store import DecisionRecord, DelegationStore
from lantern.engine.store import LANDED_RUN_STATES
from lantern.ghids import normalize_item_id

if TYPE_CHECKING:
    from lantern.api.auth.deps import Authenticated

#: The window reported on when nobody asks for another: the day a person
#: was away. The widest is the usage window's.
WINDOW_S = 86400.0
#: The runway's rate is taken over this much trailing time, whatever the
#: window asked for — a day of landings is too few to divide by.
RATE_WINDOW_S = 7 * 86400.0
#: How many landed runs and allowed decisions the briefing names.
RECENT = 10
#: Reading what was decided takes this; the counts take ``runs:read``.
DECISIONS_CAPABILITY = "audit:read"


def _title(record: Any, item: Any | None) -> str:
    if item is not None and item.title:
        return str(item.title)
    first = str(record.outcome or "").strip().splitlines()
    return first[0] if first else record.run_id


def _outcomes(views: Views, *, since: float, until: float) -> BriefingOutcomes:
    lanes: dict[str, Counter[str]] = {}
    for row in views.store.ended_between(since, until):
        lanes.setdefault(row.kind, Counter())[row.state] += row.runs
    by_kind = [
        BriefingLane(
            kind=kind,
            landed=sum(tally[state] for state in LANDED_RUN_STATES),
            failed=tally["failed"],
            cancelled=tally["cancelled"],
        )
        for kind, tally in sorted(lanes.items())
    ]
    dstore = views.dstore
    landed = views.store.landed_between(
        since, until, limit=RECENT, exclude=dstore.deleted_run_ids()
    )
    item_ids = dstore.items_for_runs([record.run_id for record in landed])
    # One query for the page's items; an id the table spells the other way
    # (a row from before typed ids) is looked up alone, as the attention
    # list does it.
    items = _items_as_stored(dstore, item_ids.values())
    recent: list[BriefingLandedRun] = []
    for record in landed:
        item = items.get(item_ids.get(record.run_id, ""))
        recent.append(
            BriefingLandedRun(
                run_id=run_public_id(record.run_id),
                kind=record.kind,
                title=_title(record, item),
                repository=item_repository(item) if item is not None else None,
                pull_request_number=record.pr_number,
                pull_request_url=record.pr_url,
                landed_at=rfc3339(record.updated_at) or "",
            )
        )
    return BriefingOutcomes(
        landed=sum(lane.landed for lane in by_kind),
        failed=sum(lane.failed for lane in by_kind),
        cancelled=sum(lane.cancelled for lane in by_kind),
        by_kind=by_kind,
        recent_landed=recent,
    )


def _decision(row: DecisionRecord, item_ids: dict[str, str]) -> BriefingDecision:
    return BriefingDecision(
        id=row.id,
        grant_id=row.grant_id,
        agent_slug=row.agent_slug,
        action=row.action,
        reason=row.reason,
        at=rfc3339(row.at) or "",
        plan_id=row.plan_id,
        node_id=row.node_id,
        item_id=item_ids.get(_item_key(row)) if row.item_id else None,
        run_id=run_public_id(row.run_id) if row.run_id else None,
        epic_run_id=row.epic_run_id,
        repository=row.repository,
        operation_id=row.operation_id,
    )


def _item_key(row: DecisionRecord) -> str:
    """The public-id key of the item a decision names: its repository and
    its id together, as ``GET /v1/decisions`` keys it."""
    return f"{row.repository or ''}|{normalize_item_id(str(row.item_id))}"


def _decided(
    views: Views, ledger: DelegationStore, *, since: float, detailed: bool
) -> BriefingDecided:
    tally = ledger.outcome_counts(since)
    recent: list[BriefingDecision] | None = None
    if detailed:
        rows = ledger.page(outcome="allow", since=since, limit=RECENT)
        keys = [_item_key(row) for row in rows if row.item_id]
        item_ids = views.ids.assign("item", keys, views.now) if keys else {}
        recent = [_decision(row, item_ids) for row in rows]
    return BriefingDecided(
        **{outcome: tally.get(outcome, 0) for outcome in OUTCOMES},
        unresolved_escalations=ledger.unresolved_count(),
        recent=recent,
    )


def _grants(ledger: DelegationStore, *, day_start: float) -> BriefingGrants:
    enabled = ledger.grants(enabled_only=True)
    used = ledger.used_today(day_start) if enabled else {}
    at_limit = sum(
        1
        for grant in enabled
        if grant.daily_limit is not None and used.get(grant.id, 0) >= grant.daily_limit
    )
    return BriefingGrants(enabled=len(enabled), at_limit=at_limit)


def briefing(views: Views, auth: Authenticated | None, *, since: float) -> Briefing:
    """The briefing for ``[since, now)`` as ``auth`` may read it. ``None``
    is the summary anyone may read (the daily digest's): every count, and
    nothing of what was decided."""
    now = views.now
    loop = views.loop
    ledger: DelegationStore = loop.delegation

    found = waiting(views)
    tally = counts(found)
    oldest = min((w.since for w in found if w.since is not None), default=None)

    supply = views.ctx.plans.store.supply()
    status = views.status()

    # The runway's rate: `code` runs landed over the trailing week, whatever
    # window was asked for — a day of landings is too few to divide by.
    landed_week = sum(
        row.runs
        for row in views.store.ended_between(now - RATE_WINDOW_S, now)
        if row.kind == "code" and row.state in LANDED_RUN_STATES
    )
    per_day = landed_week / (RATE_WINDOW_S / 86400.0) if landed_week else None

    figures = loop.usage_pool.snapshot(now)

    return Briefing(
        since=rfc3339(since) or "",
        until=rfc3339(now) or "",
        observed_at=rfc3339(now) or "",
        outcomes=_outcomes(views, since=since, until=now),
        waiting=BriefingWaiting(
            total=tally.total,
            decision=tally.decision,
            failed=tally.failed,
            paused=tally.paused,
            oldest_since=rfc3339(oldest),
        ),
        decided=_decided(
            views,
            ledger,
            since=since,
            detailed=auth is not None and DECISIONS_CAPABILITY in auth.principal.capabilities,
        ),
        supply=BriefingSupply(
            proposed=supply.proposed,
            approved=supply.approved,
            ready_tasks=supply.ready_tasks,
            queued=int(status.get("queued") or 0),
            running=len(views.live_run_ids()),
            parked=tally.decision + tally.paused,
        ),
        runway=BriefingRunway(
            ready_tasks=supply.ready_tasks,
            landed_per_day=per_day,
            days=supply.ready_tasks / per_day if per_day else None,
        ),
        budget=BriefingBudget(
            runs_today=int(figures["runs_today"]),
            max_runs_per_day=int(figures["max_runs_per_day"]),
            tokens_today=int(figures["tokens_today"]),
            daily_token_budget=figures["daily_token_budget"],
            resets_at=rfc3339(figures["resets_at"]) or "",
        ),
        grants=_grants(ledger, day_start=float(figures["day_start"])),
    )


__all__ = ["DECISIONS_CAPABILITY", "RATE_WINDOW_S", "RECENT", "WINDOW_S", "briefing"]
