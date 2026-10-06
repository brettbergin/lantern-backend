"""Goals: the standing objectives an owner writes for a repository.

A goal is direction, not work: a title and the objective in the owner's
words, for one repository, ``active``, ``paused`` or ``done``. Plans are
proposed from it and name it (``daemon_plans.goal_id``); this module reads
which plans serve a goal from those rows directly, one query for any
number of goals, never by loading every plan.

Every edit names the revision the caller read (:class:`StaleGoal` when it
moved on). Deleting a goal leaves the plans proposed from it as they are:
their ``goal_id`` keeps naming what they were proposed from.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import and_, delete, exists, func, select
from sqlalchemy.orm import aliased

from lantern.daemon.store import DaemonStore
from lantern.db.daemon_models import GoalRow, PlanNodeRow, PlanRow
from lantern.ids import _token

GOAL_PREFIX = "goal_"

GoalState = Literal["active", "paused", "done"]
GOAL_STATES: tuple[GoalState, ...] = ("active", "paused", "done")

#: The longest title and objective a goal carries.
TITLE_MAX = 200
TEXT_MAX = 4000

#: What an edit may change. The repository is a goal's identity: the
#: plans proposed from it are on that repository.
EDITABLE: frozenset[str] = frozenset({"title", "text", "state"})


def new_goal_id() -> str:
    return GOAL_PREFIX + _token(16)


class GoalGone(Exception):
    """No goal has that id (or it was deleted under the caller)."""


class StaleGoal(Exception):
    """The goal moved on since the caller read it."""

    def __init__(self, current: int) -> None:
        super().__init__(f"the goal is at revision {current}")
        self.current = current


class GoalInvalid(ValueError):
    """A goal field that is wrong, named."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


@dataclass(frozen=True, slots=True)
class Goal:
    id: str
    repository: str
    title: str
    text: str
    state: GoalState
    created_by: str | None
    created_by_display: str | None
    created_at: float
    updated_at: float
    revision: int


@dataclass(frozen=True, slots=True)
class GoalPlan:
    """One plan proposed from a goal: its id, its root's title, the state a
    plan reads as (``draft``, ``published`` or ``archived``) and its
    ``advance`` switch."""

    plan_id: str
    title: str
    state: Literal["draft", "published", "archived"]
    advance: str
    updated_at: float

    @property
    def open(self) -> bool:
        return self.state != "archived"


def check_title(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoalInvalid("title", "title must name the goal")
    title = value.strip()
    if len(title) > TITLE_MAX:
        raise GoalInvalid("title", f"title is at most {TITLE_MAX} characters")
    return title


def check_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoalInvalid("text", "text must say what the goal is")
    text = value.strip()
    if len(text) > TEXT_MAX:
        raise GoalInvalid("text", f"text is at most {TEXT_MAX} characters")
    return text


def check_state(value: object) -> GoalState:
    for state in GOAL_STATES:
        if value == state:
            return state
    raise GoalInvalid("state", f"state is one of {', '.join(GOAL_STATES)}")


def _goal(row: GoalRow) -> Goal:
    return Goal(
        id=str(row.goal_id),
        repository=str(row.repository),
        title=str(row.title),
        text=str(row.text),
        state=cast(GoalState, row.state),
        created_by=row.created_by,
        created_by_display=row.created_by_display,
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        revision=int(row.revision),
    )


class GoalStore:
    """``daemon_goals``. Each write is one transaction under the daemon
    store's lock."""

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    def goals(self, *, repository: str | None = None, state: GoalState | None = None) -> list[Goal]:
        """Every goal, oldest first, narrowed to a repository (matched
        without regard to case) and a state."""
        stmt = select(GoalRow).order_by(GoalRow.created_at.asc(), GoalRow.goal_id.asc())
        if repository is not None:
            stmt = stmt.where(func.lower(GoalRow.repository) == repository.strip().casefold())
        if state is not None:
            stmt = stmt.where(GoalRow.state == state)
        with self.dstore.read() as session:
            return [_goal(row) for row in session.scalars(stmt)]

    def goal(self, goal_id: str) -> Goal | None:
        with self.dstore.read() as session:
            row = session.get(GoalRow, goal_id)
            return None if row is None else _goal(row)

    def create(
        self,
        *,
        repository: str,
        title: str,
        text: str,
        state: GoalState = "active",
        created_by: str | None,
        created_by_display: str | None,
        now: float,
        goal_id: str | None = None,
    ) -> Goal:
        """Store a new goal at revision 1. The caller validated it;
        ``goal_id`` lets a recorded operation name the goal before it
        exists."""
        with self.dstore.transaction() as session:
            row = GoalRow(
                goal_id=goal_id or new_goal_id(),
                repository=repository,
                title=check_title(title),
                text=check_text(text),
                state=check_state(state),
                created_by=created_by,
                created_by_display=created_by_display,
                created_at=now,
                updated_at=now,
                revision=1,
            )
            session.add(row)
            session.flush()
            return _goal(row)

    def update(
        self,
        goal_id: str,
        changes: Mapping[str, Any],
        *,
        expected_revision: int,
        now: float,
    ) -> Goal:
        """Apply ``changes`` (keys of :data:`EDITABLE`) against the
        revision the caller read. :class:`GoalGone` when there is no such
        goal, :class:`StaleGoal` when it has been edited since."""
        unknown = sorted(set(changes) - EDITABLE)
        if unknown:
            raise GoalInvalid(
                unknown[0],
                f"{unknown[0]} cannot be edited; an edit may change {', '.join(sorted(EDITABLE))}",
            )
        with self.dstore.transaction() as session:
            row = session.get(GoalRow, goal_id)
            if row is None:
                raise GoalGone(goal_id)
            if int(row.revision) != expected_revision:
                raise StaleGoal(int(row.revision))
            if "title" in changes:
                row.title = check_title(changes["title"])
            if "text" in changes:
                row.text = check_text(changes["text"])
            if "state" in changes:
                row.state = check_state(changes["state"])
            row.updated_at = now
            row.revision = int(row.revision) + 1
            session.flush()
            return _goal(row)

    def delete(self, goal_id: str) -> Goal | None:
        """Remove a goal and return what it was; ``None`` when there was
        none. The plans proposed from it keep naming it."""
        with self.dstore.transaction() as session:
            row = session.get(GoalRow, goal_id)
            if row is None:
                return None
            gone = _goal(row)
            session.execute(delete(GoalRow).where(GoalRow.goal_id == goal_id))
            return gone

    # -- the plans serving a goal ---------------------------------------------------

    def plans_for(self, goal_id: str) -> list[GoalPlan]:
        """The plans proposed from ``goal_id``, most recently changed first."""
        return self.plans_by_goal([goal_id]).get(goal_id, [])

    def plans_by_goal(self, goal_ids: Iterable[str]) -> dict[str, list[GoalPlan]]:
        """The plans proposed from each of ``goal_ids``, most recently
        changed first: one query over the plans that name them, the root
        node's title joined in and whether any node is published asked
        of the database."""
        wanted = sorted(set(goal_ids))
        if not wanted:
            return {}
        node = aliased(PlanNodeRow)
        published = (
            exists()
            .where(and_(node.plan_id == PlanRow.plan_id, node.state == "published"))
            .correlate(PlanRow)
        )
        stmt = (
            select(
                PlanRow.goal_id,
                PlanRow.plan_id,
                PlanRow.state,
                PlanRow.advance,
                PlanRow.updated_at,
                PlanNodeRow.title,
                published.label("published"),
            )
            .join(PlanNodeRow, PlanNodeRow.node_id == PlanRow.root_node_id, isouter=True)
            .where(PlanRow.goal_id.in_(wanted))
            .order_by(PlanRow.updated_at.desc(), PlanRow.plan_id)
        )
        served: dict[str, list[GoalPlan]] = {}
        with self.dstore.read() as session:
            for (
                goal_id,
                plan_id,
                state,
                advance,
                updated_at,
                title,
                is_published,
            ) in session.execute(stmt):
                plan_state: Literal["draft", "published", "archived"] = (
                    "archived" if state == "archived" else "published" if is_published else "draft"
                )
                served.setdefault(str(goal_id), []).append(
                    GoalPlan(
                        plan_id=str(plan_id),
                        title=str(title or ""),
                        state=plan_state,
                        advance=str(advance or "manual"),
                        updated_at=float(updated_at),
                    )
                )
        return served


def open_plan(plans: Iterable[GoalPlan]) -> GoalPlan | None:
    """The plan currently serving a goal: the most recently changed one
    that is not archived."""
    return next((plan for plan in plans if plan.open), None)
