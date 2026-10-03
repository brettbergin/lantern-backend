"""Fleet analytics: a window of runs folded into outcomes, time and turns
— what the console's Overview shows, for any client."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from lantern.analytics import BUCKETS, WINDOW_S
from lantern.api.analytics import BUCKETS_MAX, WINDOW_MAX_S, WINDOW_MIN_S, window_analytics
from lantern.api.auth.deps import Authenticated, get_ctx, require
from lantern.api.context import ApiContext
from lantern.api.errors import Problem
from lantern.api.models import AnalyticsWindow
from lantern.api.projections import Views
from lantern.api.usage import parse_when

router = APIRouter(prefix="/v1", tags=["analytics"])

#: The year 3000. A window begins at or after 1970 and ends before this:
#: the span every host this daemon runs on can turn into a timestamp.
UNTIL_MAX = 32503680000.0


@router.get("/analytics", response_model=AnalyticsWindow)
async def get_analytics(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    window_s: Annotated[int, Query(ge=WINDOW_MIN_S, le=int(WINDOW_MAX_S))] = int(WINDOW_S),
    buckets: Annotated[int, Query(ge=1, le=BUCKETS_MAX)] = BUCKETS,
    until: Annotated[str | None, Query(max_length=64)] = None,
) -> AnalyticsWindow:
    """How the runs that began in a window went: outcomes, time to land,
    time parked on a person, turns, rework and failures by cause, per run
    kind and in total, with the window before it for comparison. The
    window is `window_s` seconds (a week when omitted, at most 90 days)
    ending at `until` (RFC 3339 or epoch; now when omitted), cut into
    `buckets` equal slices. Telemetry, never a bill."""
    now = ctx.clock()
    try:
        end = parse_when(until, default=now)
    except ValueError as exc:
        raise Problem(422, "invalid_request", str(exc)) from exc
    if not window_s <= end < UNTIL_MAX:
        # Also what a NaN fails: a bound that is not a time has no window.
        raise Problem(
            422, "invalid_request", "the window must begin in 1970 or later and end before 3000"
        )

    def read() -> AnalyticsWindow:
        return window_analytics(
            Views(ctx).store, until=end, window_s=float(window_s), buckets=buckets, now=now
        )

    return await ctx.call(read)
