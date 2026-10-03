"""What every plan module that reads or writes the forge shares.

Publishing, reconciling, a person's direct writes, a re-plan's approval
and completion each read issues through :class:`~lantern.vcs.protocol.IssueOps`
and say what went wrong in a result or a refusal. The small things they
all need live here once: how an error is said, how an issue that is gone
is told apart from a forge that refused, one issue as the forge answered
it, and how a node's issue is named.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lantern.plans.model import ForgeRef, ForgeState, Plan, PlanNode
from lantern.plans.render import repo_of

#: How long an error from the forge may run in a result or a refusal.
ERROR_MAX = 500
#: What the forge answers for an issue that is not there: GitHub's 410 for
#: a deleted one, 404 on both forges for one it cannot find.
GONE = frozenset({404, 410})


def say(exc: BaseException) -> str:
    """An exception as one line a person reads, no longer than ``ERROR_MAX``."""
    return (" ".join(str(exc).split()) or type(exc).__name__)[:ERROR_MAX]


def gone(exc: BaseException) -> bool:
    """The forge said the issue is not there (as against refusing the call)."""
    return getattr(exc, "http_status", None) in GONE


def state_of(row: Mapping[str, Any]) -> ForgeState:
    """An issue row's state as the plan records it."""
    return "closed" if str(row.get("state") or "") == "closed" else "open"


@dataclass(frozen=True, slots=True)
class Seen:
    """One issue as the forge answered it."""

    repo: str
    number: int
    url: str
    title: str
    body: str
    state: ForgeState
    updated_at: str | None


def seen_of(row: Mapping[str, Any], repo: str, number: int) -> Seen:
    updated = row.get("updated_at")
    return Seen(
        repo=repo_of(row) or repo,
        number=int(row.get("number") or number),
        url=str(row.get("html_url") or ""),
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        state=state_of(row),
        updated_at=None if updated is None else str(updated),
    )


def issue_ref(node: PlanNode) -> str:
    """``owner/name#N`` for a node on the forge; its id otherwise."""
    return f"{node.repository}#{node.forge.number}" if node.forge else node.id


def refs(plan: Plan) -> dict[str, tuple[str, ForgeRef]]:
    """Every published node's repository and issue, by node id: what a
    ``Depends on`` section renders, and what a walk already knows."""
    return {
        n.id: (n.repository, n.forge)
        for n in plan.nodes
        if n.state == "published" and n.forge is not None
    }
