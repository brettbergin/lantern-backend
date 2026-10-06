"""What is waiting on a person: one list every client reads instead of
assembling its own, and one route to act on an entry of it."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response

from lantern.api.attention import GROUPS, Waiting, counts, entries, waiting
from lantern.api.attention_act import AttentionActRequest, AttentionActResult, act
from lantern.api.auth.deps import Authenticated, get_ctx, ready_daemon, require
from lantern.api.commands import idempotency
from lantern.api.context import PAGE_DEFAULT, PAGE_MAX, ApiContext
from lantern.api.errors import Problem
from lantern.api.models import AttentionPage, rfc3339
from lantern.api.pagination import decode_cursor, encode_cursor
from lantern.api.projections import Views

router = APIRouter(prefix="/v1", tags=["attention"])

_PROBLEM = {"description": "A refusal, as `application/problem+json`."}


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
    a provider hold nothing will retry by itself, a repository whose
    polling is suspended, a `manual` plan's breakdown questions with no
    parked item standing for them (`plan_questions`) and its proposed
    levels (`plan_proposal`), and every escalation an agent left for a
    person (`escalation`, naming the `agent`, the `decision_id` and the
    `decision_action`). Decisions first, then failures, then pauses; the
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


@router.post(
    "/attention/{entry_id}/act",
    response_model=AttentionActResult,
    summary="Act on an entry",
    responses={
        201: {
            "model": AttentionActResult,
            "description": "Created: an approved escalation started an epic run.",
        },
        202: {
            "model": AttentionActResult,
            "description": "Accepted: the action's effect completes afterwards.",
        },
        403: _PROBLEM,
        409: _PROBLEM,
        422: _PROBLEM,
        503: _PROBLEM,
    },
)
async def act_on_attention(
    entry_id: str,
    body: AttentionActRequest,
    request: Request,
    response: Response,
    ctx: ApiContext = Depends(ready_daemon),  # noqa: B008
    auth: Authenticated = Depends(require("runs:read")),  # noqa: B008
) -> AttentionActResult:
    """Take one of the actions an entry of `GET /v1/attention` offers,
    naming only the entry and the action. The entry is looked up as it
    stands now (a dismissed one included, so `undismiss` works) and the
    request is handed to the command the action's own route runs: the same
    operation, the same refusals, nothing recorded for the act itself.

    `action` is one of the entry's `actions`; the caller needs that
    action's `capability` (`403 forbidden` naming it otherwise). `params`
    are the action's own arguments, validated as its route validates its
    body: `rounds` for `grant_rounds`; `text` (and `source_refs`,
    `task_id`, `agent_slug`) for `steer`; `reason` for `abandon`,
    `dismiss` and the other item actions; `reason` and
    `discard_undelivered` for `delete`; `retry` and `reason` for `cancel`;
    `rounds` for `approve` on an escalated round grant.

    On an `escalation`, `approve` takes the step the agent asked to take
    as the caller, through that step's own command and operation, then
    resolves the decision `acted`; `decline` records a `decision.decline`
    operation resolving it `declined`. Either needs the capability the
    entry lists (the escalated step's). On a `plan_proposal`, `approve`
    approves the level (`plans:create`). `decision` carries the ledger
    row after an act on an escalation.

    `expected_revision` is the entry's `revision` as the person read it,
    never defaulted from the entry as it stands. `gate_approve` requires
    it (`422` without). On a `gate_approve`, and on an item action of an
    `item` entry, the command itself checks it and records the refusal;
    on any other action the entry's revision is compared before the
    command runs. Either way a moved entry is `409 stale_revision` with
    the current `revision`. An entry whose `revision` is `null` — an
    `epic_task`, a `repository` — takes none (`422`).

    The `Idempotency-Key` header is required (`422
    idempotency_key_required`), scoped to the caller and this entry. A
    replay answers the first act (`replayed: true`), also once the entry
    is gone; another action, or other `params`, under the same key is
    `409 idempotency_conflict`.

    An id nothing is waiting under is `409 not_waiting`: the thing was
    settled, or waits again as a new entry. An action the entry does not
    offer now is `409 not_eligible` with `offered`; a name that is no
    action is `422 unknown_action`. Every refusal of the action's own
    route can be answered too.

    `200` with `{entry_id, action, operation_id, replayed, still_waiting,
    result}`; `202` where the action's own route answers `202` (a gate
    approval, a steer, a cancel honoured at the run's next boundary, an
    approved breakdown) and `201` where it answers `201` (an approved
    epic run start).
    `result` is the body the action's own route answers and
    `still_waiting` whether the entry is still on the list (dismissed
    alerts left out) as the answer is written."""
    pair = idempotency(request, auth.principal, f"/v1/attention/{entry_id}/act", required=True)
    assert pair is not None  # nosec B101 - required above
    result, status = await act(ctx, auth, entry_id, body, pair)
    response.status_code = status
    if result.operation_id:
        response.headers["Location"] = f"/v1/operations/{result.operation_id}"
    return result
