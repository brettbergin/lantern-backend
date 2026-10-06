"""Work items: what the daemon has been asked to do, the queue in dispatch
order, and admitting or settling an item (#1036)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from fastapi import APIRouter, Depends, Query, Request, Response

from lantern.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from lantern.api.commands import admit, idempotency, item_command
from lantern.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from lantern.api.errors import Problem
from lantern.api.models import (
    Admitted,
    IntakeRequest,
    Item,
    ItemCommand,
    ItemCommandResult,
    ItemDetail,
    QueuePage,
    WorkDeleteCommand,
    rfc3339,
)
from lantern.api.pagination import Page, decode_cursor, encode_cursor
from lantern.api.projections import Views
from lantern.daemon.model import ItemState, WorkItem

router = APIRouter(prefix="/v1", tags=["items"])

ITEM_STATES: tuple[str, ...] = get_args(ItemState)
RUN_KINDS: tuple[str, ...] = ("code", "workload", "tool", "plan")


@router.get("/items", response_model=Page[Item])
async def list_items(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    state: Annotated[list[str] | None, Query()] = None,
    kind: Annotated[list[str] | None, Query()] = None,
    repository_id: Annotated[str | None, Query()] = None,
    include_deleted: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
    cursor: Annotated[str | None, Query()] = None,
) -> Page[Item]:
    """Work items, newest first, filterable by state, kind and repository.
    Items a person deleted are left out unless ``include_deleted``."""
    states = [s for s in state or [] if s in ITEM_STATES]
    if state and len(states) != len(state):
        raise Problem(422, "invalid_request", f"state must be one of {', '.join(ITEM_STATES)}")
    kinds = [k for k in kind or [] if k in RUN_KINDS]
    if kind and len(kinds) != len(kind):
        raise Problem(422, "invalid_request", f"kind must be one of {', '.join(RUN_KINDS)}")
    filters: dict[str, Any] = {
        "state": sorted(states),
        "kind": sorted(kinds),
        "repository_id": repository_id,
        # Only when asked for, so a cursor issued before this filter
        # existed still reads the listing it was issued for.
        **({"include_deleted": True} if include_deleted else {}),
    }
    after: tuple[float, str] | None = None
    if cursor is not None:
        key = decode_cursor(cursor, filters)
        try:
            after = (float(key["c"]), str(key["i"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise Problem(400, "invalid_cursor", "the cursor is malformed") from exc

    def read() -> tuple[list[Item], list[WorkItem], bool]:
        views = Views(ctx)
        repo = views.repository_by_public_id(repository_id).repo if repository_id else None
        rows: list[WorkItem] = views.dstore.page_items(
            states=states or None,
            kinds=kinds or None,
            repo=repo,
            after=after,
            limit=limit + 1,
            include_deleted=include_deleted,
        )
        more = len(rows) > limit
        rows = rows[:limit]
        channels = views.visible_item_channels(rows, _auth.member)
        data = [
            item.model_copy(update={"channel_id": channels.get(row.item_id)})
            for item, row in zip(views.items(rows), rows, strict=True)
        ]
        return data, rows, more

    data, rows, more = await ctx.call(read)
    next_cursor = (
        encode_cursor({"c": rows[-1].created_at, "i": rows[-1].item_id}, filters)
        if more and rows
        else None
    )
    return Page(data=data, next_cursor=next_cursor, has_more=more)


@router.get("/items/{item_id}", response_model=ItemDetail)
async def get_item(
    item_id: str,
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> ItemDetail:
    """The request, its origin, its state, and every run it had."""

    def read() -> ItemDetail:
        views = Views(ctx)
        item = views.item_by_public_id(item_id)
        channels = views.visible_item_channels([item], _auth.member)
        return views.item_detail(item).model_copy(update={"channel_id": channels.get(item.item_id)})

    return await ctx.call(read)


@router.get("/queue", response_model=QueuePage)
async def queue(
    ctx: ApiContext = Depends(get_ctx),  # noqa: B008
    _auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
    limit: Annotated[int, Query(ge=1, le=PAGE_MAX)] = PAGE_DEFAULT,
) -> QueuePage:
    """The queue in the order dispatch considers it — interrupted runs
    awaiting resume first, then oldest first — with each entry's own
    eligibility and what holds the whole queue."""

    def read() -> QueuePage:
        views = Views(ctx)
        entries, more = views.queue(limit)
        status = views.status()
        return QueuePage(
            data=entries,
            has_more=more,
            observed_at=rfc3339(views.now) or "",
            paused=bool(status.get("paused")),
            breaker_open=bool(status.get("breaker_open")),
        )

    return await ctx.call(read)


# -- intake ----------------------------------------------------------------------


@router.post("/items", response_model=Admitted, status_code=201)
async def admit_item(
    body: IntakeRequest,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("items:create")),  # noqa: B008
) -> Admitted:
    """Admit work through its source's rules: an existing issue in a
    configured repository (labelled as a person would, so polling
    converges on the same item), an inline workload under an allowed
    profile, or a registered tool recipe with validated parameters. An
    ``Idempotency-Key`` is required; a replay answers with the same item
    and operation. ``201`` when the item was created, ``200`` when it was
    already queued."""
    pair = idempotency(request, auth.principal, "/v1/items", required=True)
    admitted = await admit(ctx, auth, body, pair)
    response.status_code = 201 if admitted.created else 200
    response.headers["Location"] = f"/v1/items/{admitted.item.id}"
    return admitted


# -- item commands ---------------------------------------------------------------


async def _item_command(
    verb: Literal["retry", "requeue", "abandon", "dismiss", "undismiss", "delete"],
    public_id: str,
    body: ItemCommand | WorkDeleteCommand | None,
    request: Request,
    ctx: ApiContext,
    auth: Authenticated,
) -> ItemCommandResult:
    pair = idempotency(request, auth.principal, f"/v1/items/{public_id}/{verb}", required=False)
    return await item_command(ctx, auth, verb, public_id, body, pair)


@router.post("/items/{item_id}/retry", response_model=ItemCommandResult)
async def retry_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """A fresh attempt for a settled item: attempts reset, the run
    unpinned, the source told who asked."""
    return await _item_command("retry", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/requeue", response_model=ItemCommandResult)
async def requeue_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Unpin the item's run so its next dispatch starts fresh, attempts
    intact; the run in flight is cancelled."""
    return await _item_command("requeue", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/abandon", response_model=ItemCommandResult)
async def abandon_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Give the item up with an attributed reason; the source hears the
    ordinary abandon report."""
    return await _item_command("abandon", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/dismiss", response_model=ItemCommandResult)
async def dismiss_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Acknowledge the alert this item raises, for everyone: it keeps its
    state and its controls and stops asking for attention. Refused for an
    item that raises none (queued, running, done). The dismissal ends by
    itself when the item changes state, so a retry that fails is a new
    alert; dismissing twice answers with the first dismissal."""
    return await _item_command("dismiss", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/undismiss", response_model=ItemCommandResult)
async def undismiss_item(
    item_id: str,
    request: Request,
    body: ItemCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Take a dismissal back: the item asks for attention again."""
    return await _item_command("undismiss", item_id, body, request, ctx, auth)


@router.post("/items/{item_id}/delete", response_model=ItemCommandResult)
async def delete_item(
    item_id: str,
    request: Request,
    body: WorkDeleteCommand | None = None,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:control")),  # noqa: B008
) -> ItemCommandResult:
    """Put a finished item away: it and its runs leave every listing, and
    their sandboxes and run directories are removed. The records and the
    event trail stay, readable by id, and nothing on the forge is touched.
    Refused for an item that is not at rest (abandon it first), and —
    unless ``discard_undelivered`` — when a run's workspace is the only
    copy of work that was never delivered. Deleting twice answers with the
    item as it stands."""
    return await _item_command("delete", item_id, body, request, ctx, auth)
