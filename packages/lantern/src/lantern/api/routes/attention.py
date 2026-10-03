"""What is waiting on a person: one list every client reads instead of
assembling its own."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from lantern.api.attention import GROUPS, Waiting, counts, entries, waiting
from lantern.api.auth.deps import Authenticated, get_ctx, require
from lantern.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from lantern.api.errors import Problem
from lantern.api.models import AttentionPage, rfc3339
from lantern.api.pagination import decode_cursor, encode_cursor
from lantern.api.projections import Views

router = APIRouter(prefix="/v1", tags=["attention"])


@router.get("/attention", response_model=AttentionPage)
async def list_attention(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    group: Annotated[list[str] | None, Query()] = None,
    repository_id: Annotated[str | None, Query()] = None,
    include_dismissed: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query()] = None,
) -> AttentionPage:
    """Everything waiting on a person, computed as it stands now: an open
    merge or publication gate (one entry with the item it parks), an item
    waiting for a review or for answers, an item that ended `failed` or
    `blocked`, a failed task of a live epic run (one entry with its item),
    a provider hold nothing will retry by itself, and a repository whose
    polling is suspended. Decisions first, then failures, then pauses; the
    longest wait first within each. A dismissed alert is left out unless
    `include_dismissed`; deleted work never appears. Each entry names what
    it is about, carries the revision an act on it is checked against, and
    lists the actions the server offers on it with the capability each
    needs and whether the caller holds it. `counts` covers every group
    whatever `group` the page was narrowed to. `kind` is open: leave out
    an entry whose kind you do not know."""
    groups = [g for g in GROUPS if g in (group or [])]
    if group and set(group) - set(GROUPS):
        raise Problem(422, "invalid_request", f"group must be one of {', '.join(GROUPS)}")
    filters: dict[str, Any] = {
        "group": groups,
        "repository_id": repository_id,
        "include_dismissed": include_dismissed,
    }
    after: tuple[int, float, str, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (int(key["g"]), float(key["s"]), str(key["k"]), str(key["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc

    def read() -> AttentionPage:
        views = Views(ctx)
        repo = views.repository_by_public_id(repository_id).repo if repository_id else None
        found: list[Waiting] = waiting(views, include_dismissed=include_dismissed)
        if repo is not None:
            found = [
                w for w in found if w.repo is not None and w.repo.casefold() == repo.casefold()
            ]
        tally = counts(found)
        listed = sorted(
            (w for w in found if not groups or w.group in groups), key=lambda w: w.order
        )
        if after is not None:
            listed = [w for w in listed if w.order > after]
        more = len(listed) > limit
        page = listed[:limit]
        last = page[-1].order if more and page else None
        return AttentionPage(
            data=entries(views, page, auth),
            next_cursor=(
                encode_cursor({"g": last[0], "s": last[1], "k": last[2], "i": last[3]}, filters)
                if last is not None
                else None
            ),
            has_more=more,
            counts=tally,
            observed_at=rfc3339(views.now) or "",
        )

    return await ctx.call(read)
