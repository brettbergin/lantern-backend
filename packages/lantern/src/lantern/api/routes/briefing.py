"""The briefing: what happened, what needs a person, what is lined up —
one request for a landing screen, a widget or a digest."""

from __future__ import annotations

import math
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from lantern.api.auth.deps import Authenticated, get_ctx, require
from lantern.api.briefing import WINDOW_S, briefing
from lantern.api.context import ApiContext
from lantern.api.errors import Problem
from lantern.api.models import Briefing
from lantern.api.projections import Views
from lantern.api.usage import WINDOW_MAX_S, parse_when

router = APIRouter(prefix="/v1", tags=["briefing"])


@router.get("/briefing", response_model=Briefing)
async def get_briefing(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    since: Annotated[str | None, Query(max_length=64)] = None,
) -> Briefing:
    """What happened since `since` (RFC 3339 or epoch; a day ago when
    omitted; at most 90 days back), what is waiting on a person now, and
    how much work is lined up — computed on read, nothing stored. A run
    counts in the window when it *finished* in it. `decided.recent` is
    detailed only for a caller holding `audit:read` and `null` otherwise;
    every other field is for everyone with `runs:read`. Clients ignore
    fields they do not know: a later release adds some."""
    now = ctx.clock()
    try:
        start = parse_when(since, default=now - WINDOW_S)
    except ValueError as exc:
        raise Problem(422, "invalid_request", str(exc)) from exc
    if math.isnan(start) or start < 0 or start >= now:
        raise Problem(422, "invalid_request", "since must be a time before now, in 1970 or later")
    if now - start > WINDOW_MAX_S:
        raise Problem(422, "invalid_request", "a briefing reaches back at most 90 days")

    def read() -> Briefing:
        return briefing(Views(ctx), auth, since=start)

    return await ctx.call(read)
