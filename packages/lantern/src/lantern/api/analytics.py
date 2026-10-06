"""Fleet analytics as a client reads them: the fold in
:mod:`lantern.analytics`, flattened.

The fold's derived values — the share that landed, the time parked, the
medians, the change against the window before — are properties, and a
property does not serialise. Everything a dashboard draws is a field here,
so no client recomputes one and gets a different answer than the console
shows. Durations are seconds (``*_s``), runs carry their public ids, and
what cannot be said stays ``null``: a success rate with no judged run, a
median with no run, a change from nothing. No currency appears anywhere;
turns and tokens are what a backend reported, not a bill.
"""

from __future__ import annotations

from collections.abc import Sequence

from lantern import analytics
from lantern.analytics import Analytics, Lane, RunRow, WindowStore
from lantern.api.models import (
    AnalyticsBucket,
    AnalyticsDelta,
    AnalyticsFailure,
    AnalyticsLane,
    AnalyticsPhase,
    AnalyticsRework,
    AnalyticsRun,
    AnalyticsSpread,
    AnalyticsSpreads,
    AnalyticsWindow,
    rfc3339,
)
from lantern.api.publicids import run_public_id
from lantern.api.usage import WINDOW_MAX_S

#: How narrow a window may be, and how many buckets it may be cut into, so
#: a response stays bounded. The widest window is the usage window's.
WINDOW_MIN_S = 60
BUCKETS_MAX = 90

#: The public name of each compared value, and the fold's own name for it.
_DELTA_METRICS: dict[str, str] = {
    "runs": "runs",
    "landed": "landed",
    "failed": "failed",
    "cancelled": "cancelled",
    "turns": "turns",
    "tokens": "tokens",
    "cache_read_tokens": "cache",
    "active_s": "active",
    "elapsed_s": "elapsed",
    "parked_s": "parked",
    "ok_rate": "ok_rate",
    "parked_share": "parked_share",
}


def lane(folded: Lane) -> AnalyticsLane:
    return AnalyticsLane(
        kind=folded.kind,
        runs=folded.runs,
        landed=folded.landed,
        failed=folded.failed,
        cancelled=folded.cancelled,
        turns=folded.turns,
        tokens=folded.tokens,
        cache_read_tokens=folded.cache,
        active_s=folded.active,
        elapsed_s=folded.elapsed,
        parked_s=folded.parked,
        ok_rate=folded.ok_rate,
        parked_share=folded.parked_share,
    )


def _run(row: RunRow) -> AnalyticsRun:
    return AnalyticsRun(
        run_id=run_public_id(row.run_id),
        kind=row.kind,
        state=row.state,
        turns=row.turns,
        tokens=row.tokens,
        active_s=row.active,
        parked_s=row.parked,
    )


def _spread(folded: Analytics, values: Sequence[float]) -> AnalyticsSpread | None:
    """Median and p90, or ``None`` when no run gives one — the fold's own
    ``(0, 0)`` for an empty list would read as a measured zero."""
    if not values:
        return None
    median, p90 = folded.spread(values)
    return AnalyticsSpread(median=median, p90=p90)


def _buckets(folded: Analytics) -> list[AnalyticsBucket]:
    count = len(folded.days)
    width = (folded.until - folded.since) / count if count else 0.0
    runs = folded.daily.get("runs", ())
    turns = folded.daily.get("turns", ())
    return [
        AnalyticsBucket(
            since=rfc3339(folded.since + index * width) or "",
            # The last bucket ends where the window does, to the digit.
            until=rfc3339(
                folded.until if index == count - 1 else folded.since + (index + 1) * width
            )
            or "",
            runs=runs[index],
            landed=day.landed,
            failed=day.failed,
            cancelled=day.cancelled,
            turns=turns[index],
        )
        for index, day in enumerate(folded.days)
    ]


def window(folded: Analytics, *, now: float) -> AnalyticsWindow:
    """One folded window as its public shape."""
    rework = folded.rework
    return AnalyticsWindow(
        since=rfc3339(folded.since) or "",
        until=rfc3339(folded.until) or "",
        observed_at=rfc3339(now) or "",
        window_s=round(folded.until - folded.since),
        empty=folded.empty,
        total=lane(folded.total),
        lanes=[lane(folded.lanes[kind]) for kind in sorted(folded.lanes)],
        phases=[
            AnalyticsPhase(
                phase=p.phase,
                attempts=p.attempts,
                retries=p.retries,
                turns=p.turns,
                tokens=p.tokens,
                cache_read_tokens=p.cache,
                active_s=p.seconds,
            )
            for p in folded.phases
        ],
        buckets=_buckets(folded),
        rework=AnalyticsRework(
            tasks=rework.tasks,
            revisions=rework.revisions,
            replans=rework.replans,
            suspect=rework.suspect,
            retried_share=rework.retried_share,
        ),
        review_rounds=folded.review_rounds,
        ci_rounds=folded.ci_rounds,
        failures=[
            AnalyticsFailure(reason=reason, count=count) for reason, count in folded.failures
        ],
        costliest=[_run(row) for row in folded.costliest],
        longest_parked=[_run(row) for row in folded.longest_parked],
        spreads=AnalyticsSpreads(
            turns=_spread(folded, [float(r.turns) for r in folded.runs_seen]),
            cycle_s=_spread(folded, [r.active + r.parked for r in folded.landed_runs]),
            active_s=_spread(folded, [r.active for r in folded.runs_seen]),
        ),
        previous=lane(folded.previous) if folded.previous is not None else None,
        delta=AnalyticsDelta(
            **{public: folded.delta(metric) for public, metric in _DELTA_METRICS.items()}
        ),
    )


def window_analytics(
    store: WindowStore, *, until: float, window_s: float, buckets: int, now: float
) -> AnalyticsWindow:
    """The window of ``window_s`` seconds ending at ``until``, read and
    folded as the console reads it, with the window before it for the
    comparison."""
    folded = analytics.compute(store, now=until, window_s=window_s, buckets=buckets)
    return window(folded, now=now)


__all__ = ["BUCKETS_MAX", "WINDOW_MAX_S", "WINDOW_MIN_S", "lane", "window", "window_analytics"]
