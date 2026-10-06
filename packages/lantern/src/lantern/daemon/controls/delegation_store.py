"""Grants and the decisions ledger, in the daemon's store.

:mod:`lantern.daemon.controls.delegation` judges; this module keeps what
it judges from and what it answered. A grant is edited against the
revision the caller read (:class:`StaleGrant` when it moved on). A decision
is written once and never edited, except that an escalation is *resolved*
once — the first resolution stands.

How many times a grant allowed something today is not a counter on the
grant: it is counted from the ledger's ``allow`` rows, so the number and
the audit trail cannot disagree, and deleting a grant loses neither.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from sqlalchemy import and_, delete, func, insert, or_, select

from lantern.daemon.controls.delegation import (
    Conditions,
    Decision,
    DecisionOutcome,
    Grant,
    GrantSource,
)
from lantern.daemon.controls.delegation_defaults import (
    SEEDED_BY,
    SEEDED_BY_DISPLAY,
    SEEDED_PREFIX,
    DefaultGrant,
)
from lantern.daemon.store import DaemonStore
from lantern.db.daemon_models import DaemonStateRow, DecisionRow, GrantRow
from lantern.ids import _token

GRANT_PREFIX = "grant_"
DECISION_PREFIX = "dec_"

#: How an escalation ended: the step happened (whoever took it), a person
#: declined it, or what it was about changed and the question lapsed.
Resolution = Literal["acted", "declined", "superseded"]

#: What an edit may change. The agent and the action are a grant's
#: identity: the ledger's rows name the grant, and a grant that came to
#: mean something else would rewrite what they say was allowed.
EDITABLE: frozenset[str] = frozenset({"conditions", "daily_limit", "enabled", "note"})


def new_grant_id() -> str:
    return GRANT_PREFIX + _token(16)


def new_decision_id() -> str:
    return DECISION_PREFIX + _token(16)


class GrantGone(Exception):
    """No grant has that id (or it was deleted under the caller)."""


class StaleGrant(Exception):
    """The grant moved on since the caller read it."""

    def __init__(self, current: int) -> None:
        super().__init__(f"the grant is at revision {current}")
        self.current = current


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One row of the ledger. The target refs are the daemon's own ids
    (an item's ``item_id`` beside the ``repository`` it belongs to)."""

    id: str
    agent_slug: str
    action: str
    outcome: DecisionOutcome
    reason: str
    at: float
    grant_id: str | None = None
    plan_id: str | None = None
    node_id: str | None = None
    item_id: str | None = None
    run_id: str | None = None
    epic_run_id: str | None = None
    repository: str | None = None
    operation_id: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    resolved_at: float | None = None
    resolved_by: str | None = None
    resolution: Resolution | None = None

    @property
    def unresolved(self) -> bool:
        """An escalation still waiting for a person."""
        return self.outcome == "escalate" and self.resolved_at is None


def _grant(row: GrantRow) -> Grant:
    return Grant(
        id=str(row.grant_id),
        agent_slug=str(row.agent_slug),
        action=str(row.action),
        conditions=Conditions.model_validate(json.loads(row.conditions_json or "{}")),
        daily_limit=None if row.daily_limit is None else int(row.daily_limit),
        enabled=bool(row.enabled),
        note=row.note,
        created_by=row.created_by,
        created_by_display=row.created_by_display,
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        revision=int(row.revision),
        source=cast(GrantSource, row.source or "owner"),
        default_key=row.default_key,
    )


def _decision(row: DecisionRow) -> DecisionRecord:
    return DecisionRecord(
        id=str(row.decision_id),
        agent_slug=str(row.agent_slug),
        action=str(row.action),
        outcome=cast(DecisionOutcome, row.outcome),
        reason=str(row.reason),
        at=float(row.at),
        grant_id=row.grant_id,
        plan_id=row.plan_id,
        node_id=row.node_id,
        item_id=row.item_id,
        run_id=row.run_id,
        epic_run_id=row.epic_run_id,
        repository=row.repository,
        operation_id=row.operation_id,
        attrs=dict(json.loads(row.attrs_json or "{}")),
        resolved_at=None if row.resolved_at is None else float(row.resolved_at),
        resolved_by=row.resolved_by,
        resolution=cast("Resolution | None", row.resolution),
    )


def _conditions_json(conditions: Conditions) -> str:
    return json.dumps(conditions.as_dict(), sort_keys=True)


class DelegationStore:
    """``daemon_grants`` and ``daemon_decisions``. Each write is one
    transaction under the daemon store's lock."""

    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    # -- grants ---------------------------------------------------------------------

    def grants(
        self,
        *,
        agent_slug: str | None = None,
        action: str | None = None,
        enabled_only: bool = False,
    ) -> list[Grant]:
        """Every grant, oldest first (the order :func:`decide` picks in)."""
        stmt = select(GrantRow).order_by(GrantRow.created_at.asc(), GrantRow.grant_id.asc())
        if agent_slug is not None:
            stmt = stmt.where(func.lower(GrantRow.agent_slug) == agent_slug.strip().casefold())
        if action is not None:
            stmt = stmt.where(GrantRow.action == action)
        if enabled_only:
            stmt = stmt.where(GrantRow.enabled != 0)
        with self.dstore.read() as session:
            return [_grant(row) for row in session.scalars(stmt)]

    def grant(self, grant_id: str) -> Grant | None:
        with self.dstore.read() as session:
            row = session.get(GrantRow, grant_id)
            return None if row is None else _grant(row)

    def create_grant(
        self,
        *,
        agent_slug: str,
        action: str,
        conditions: Conditions,
        daily_limit: int | None,
        enabled: bool,
        note: str | None,
        created_by: str | None,
        created_by_display: str | None,
        now: float,
        grant_id: str | None = None,
    ) -> Grant:
        """Store a new grant at revision 1. The caller has validated it
        (:func:`~lantern.daemon.controls.delegation.parse_conditions`);
        ``grant_id`` lets a recorded operation name the grant before it
        exists."""
        with self.dstore.transaction() as session:
            row = GrantRow(
                grant_id=grant_id or new_grant_id(),
                agent_slug=agent_slug,
                action=action,
                conditions_json=_conditions_json(conditions),
                daily_limit=daily_limit,
                enabled=1 if enabled else 0,
                note=note,
                created_by=created_by,
                created_by_display=created_by_display,
                created_at=now,
                updated_at=now,
                revision=1,
            )
            session.add(row)
            session.flush()
            return _grant(row)

    def seed_defaults(
        self, defaults: Iterable[DefaultGrant], *, now: float, restore: bool = False
    ) -> list[Grant]:
        """Write the defaults that are due, in one transaction holding the
        write lock, and return the grants written.

        At start (``restore`` false) a default is due when its key was
        never seeded: one seeded before is not written again, even when
        its grant is gone — the owner deleted it. On a restore a default
        is due when no grant carries its key, whatever was seeded. Either
        way an existing grant is never touched, each key written is
        recorded as seeded, and the unique ``default_key`` index keeps a
        second process from writing one twice."""
        written: list[Grant] = []
        with self.dstore.immediate_transaction() as session:
            seeded = {
                str(key).removeprefix(SEEDED_PREFIX)
                for key in session.scalars(
                    select(DaemonStateRow.key).where(
                        DaemonStateRow.key.startswith(SEEDED_PREFIX, autoescape=True)
                    )
                )
            }
            present = {
                str(key)
                for key in session.scalars(
                    select(GrantRow.default_key).where(GrantRow.default_key.is_not(None))
                )
            }
            for default in defaults:
                if default.key in present or (not restore and default.key in seeded):
                    continue
                row = GrantRow(
                    grant_id=new_grant_id(),
                    agent_slug=default.agent_slug,
                    action=default.action,
                    conditions_json=_conditions_json(default.parsed()),
                    daily_limit=default.daily_limit,
                    enabled=1,
                    note=default.note,
                    created_by=SEEDED_BY,
                    created_by_display=SEEDED_BY_DISPLAY,
                    created_at=now,
                    updated_at=now,
                    revision=1,
                    source="default",
                    default_key=default.key,
                )
                session.add(row)
                if default.key not in seeded:
                    session.execute(
                        insert(DaemonStateRow)
                        .prefix_with("OR REPLACE")
                        .values(key=SEEDED_PREFIX + default.key, value=repr(now))
                    )
                present.add(default.key)
                session.flush()
                written.append(_grant(row))
        return written

    def seeded_default_keys(self) -> set[str]:
        """Every default key ever seeded here, whether its grant remains."""
        values = self.dstore.values_with_prefix(SEEDED_PREFIX)
        return {key.removeprefix(SEEDED_PREFIX) for key in values}

    def update_grant(
        self,
        grant_id: str,
        changes: Mapping[str, Any],
        *,
        expected_revision: int,
        now: float,
    ) -> Grant:
        """Apply ``changes`` (keys of :data:`EDITABLE`) against the
        revision the caller read. :class:`GrantGone` when there is no such
        grant, :class:`StaleGrant` when it has been edited since."""
        unknown = sorted(set(changes) - EDITABLE)
        if unknown:
            raise ValueError(
                f"{', '.join(unknown)} cannot be edited; an edit may change "
                f"{', '.join(sorted(EDITABLE))}"
            )
        with self.dstore.transaction() as session:
            row = session.get(GrantRow, grant_id)
            if row is None:
                raise GrantGone(grant_id)
            if int(row.revision) != expected_revision:
                raise StaleGrant(int(row.revision))
            if "conditions" in changes:
                row.conditions_json = _conditions_json(changes["conditions"])
            if "daily_limit" in changes:
                row.daily_limit = changes["daily_limit"]
            if "enabled" in changes:
                row.enabled = 1 if changes["enabled"] else 0
            if "note" in changes:
                row.note = changes["note"]
            row.updated_at = now
            row.revision = int(row.revision) + 1
            session.flush()
            return _grant(row)

    def delete_grant(self, grant_id: str) -> Grant | None:
        """Remove a grant and return what it was; ``None`` when there was
        none. The decisions it allowed stay in the ledger."""
        with self.dstore.transaction() as session:
            row = session.get(GrantRow, grant_id)
            if row is None:
                return None
            gone = _grant(row)
            session.execute(delete(GrantRow).where(GrantRow.grant_id == grant_id))
            return gone

    # -- decisions ------------------------------------------------------------------

    def record(
        self,
        decision: Decision,
        *,
        agent_slug: str,
        action: str,
        attrs: Mapping[str, Any],
        now: float,
        plan_id: str | None = None,
        node_id: str | None = None,
        item_id: str | None = None,
        run_id: str | None = None,
        epic_run_id: str | None = None,
        repository: str | None = None,
        operation_id: str | None = None,
    ) -> DecisionRecord:
        """Write what the judge answered, with the facts it answered on.

        ``repository`` defaults to the ``repository`` fact; name it when
        the act is about an item, whose id is only unique beside its
        repository."""
        if repository is None:
            named = attrs.get("repository")
            repository = named if isinstance(named, str) and named else None
        with self.dstore.transaction() as session:
            row = DecisionRow(
                decision_id=new_decision_id(),
                grant_id=decision.grant_id,
                agent_slug=agent_slug,
                action=action,
                outcome=decision.outcome,
                reason=decision.reason,
                plan_id=plan_id,
                node_id=node_id,
                item_id=item_id,
                run_id=run_id,
                epic_run_id=epic_run_id,
                repository=repository,
                operation_id=operation_id,
                attrs_json=json.dumps(dict(attrs), sort_keys=True, default=str),
                at=now,
            )
            session.add(row)
            session.flush()
            return _decision(row)

    def decision(self, decision_id: str) -> DecisionRecord | None:
        with self.dstore.read() as session:
            row = session.get(DecisionRow, decision_id)
            return None if row is None else _decision(row)

    def page(
        self,
        *,
        outcome: str | None = None,
        unresolved: bool = False,
        agent_slug: str | None = None,
        since: float | None = None,
        after: tuple[float, str] | None = None,
        limit: int = 50,
    ) -> list[DecisionRecord]:
        """A page newest first, keyed on ``(at, id)``: ``after`` is the
        last row of the previous page. ``unresolved`` keeps only the
        escalations still waiting for a person; ``since`` keeps what was
        decided at or after that time."""
        stmt = (
            select(DecisionRow)
            .order_by(DecisionRow.at.desc(), DecisionRow.decision_id.desc())
            .limit(limit)
        )
        if outcome is not None:
            stmt = stmt.where(DecisionRow.outcome == outcome)
        if unresolved:
            stmt = stmt.where(DecisionRow.outcome == "escalate", DecisionRow.resolved_at.is_(None))
        if agent_slug is not None:
            stmt = stmt.where(func.lower(DecisionRow.agent_slug) == agent_slug.strip().casefold())
        if since is not None:
            stmt = stmt.where(DecisionRow.at >= since)
        if after is not None:
            at, decision_id = after
            stmt = stmt.where(
                or_(
                    DecisionRow.at < at,
                    and_(DecisionRow.at == at, DecisionRow.decision_id < decision_id),
                )
            )
        with self.dstore.read() as session:
            return [_decision(row) for row in session.scalars(stmt)]

    def latest(self, *, action: str, plan_id: str, node_id: str) -> DecisionRecord | None:
        """The newest decision on ``action`` for one plan node, whatever its
        outcome: what the plan driver compares a fresh judgement with, so
        the same answer to the same situation is written once."""
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.action == action,
                DecisionRow.plan_id == plan_id,
                DecisionRow.node_id == node_id,
            )
            .order_by(DecisionRow.at.desc(), DecisionRow.decision_id.desc())
            .limit(1)
        )
        with self.dstore.read() as session:
            row = session.scalars(stmt).first()
            return None if row is None else _decision(row)

    def latest_for_goal(
        self, goal_id: str, *, action: str = "plan.propose", outcome: str | None = None
    ) -> DecisionRecord | None:
        """The newest decision on ``action`` about one goal (its
        ``goal_id`` fact), narrowed to an ``outcome``: what the proposer
        compares a fresh judgement with, and when it last proposed. A
        proposal is about a goal, not yet a plan, so the goal rides in the
        facts; the match is confirmed on the parsed facts, never on the
        text alone."""
        needle = json.dumps({"goal_id": goal_id})[1:-1]
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.action == action,
                DecisionRow.attrs_json.contains(needle, autoescape=True),
            )
            .order_by(DecisionRow.at.desc(), DecisionRow.decision_id.desc())
        )
        if outcome is not None:
            stmt = stmt.where(DecisionRow.outcome == outcome)
        with self.dstore.read() as session:
            for row in session.scalars(stmt):
                record = _decision(row)
                if record.attrs.get("goal_id") == goal_id:
                    return record
        return None

    def unresolved_for_action(self, action: str) -> list[DecisionRecord]:
        """The escalations on ``action`` still waiting, oldest first."""
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.action == action,
                DecisionRow.outcome == "escalate",
                DecisionRow.resolved_at.is_(None),
            )
            .order_by(DecisionRow.at.asc(), DecisionRow.decision_id.asc())
        )
        with self.dstore.read() as session:
            return [_decision(row) for row in session.scalars(stmt)]

    def proposal_for(self, plan_id: str) -> DecisionRecord | None:
        """The allowed ``plan.propose`` that drafted ``plan_id``, or
        ``None`` when the ledger has none (a plan a person drafted, or a
        proposal whose decision was never written)."""
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.action == "plan.propose",
                DecisionRow.outcome == "allow",
                DecisionRow.plan_id == plan_id,
            )
            .order_by(DecisionRow.at.desc(), DecisionRow.decision_id.desc())
            .limit(1)
        )
        with self.dstore.read() as session:
            row = session.scalars(stmt).first()
            return None if row is None else _decision(row)

    def unresolved_for_plan(self, plan_id: str) -> list[DecisionRecord]:
        """The escalations about ``plan_id`` still waiting, oldest first."""
        stmt = (
            select(DecisionRow)
            .where(
                DecisionRow.plan_id == plan_id,
                DecisionRow.outcome == "escalate",
                DecisionRow.resolved_at.is_(None),
            )
            .order_by(DecisionRow.at.asc(), DecisionRow.decision_id.asc())
        )
        with self.dstore.read() as session:
            return [_decision(row) for row in session.scalars(stmt)]

    def outcome_counts(self, since: float) -> dict[str, int]:
        """``outcome -> how many`` decisions were taken at or after
        ``since``, in one grouped query; an outcome nothing was decided
        under is absent."""
        stmt = (
            select(DecisionRow.outcome, func.count())
            .where(DecisionRow.at >= since)
            .group_by(DecisionRow.outcome)
        )
        with self.dstore.read() as session:
            return {str(outcome): int(count) for outcome, count in session.execute(stmt)}

    def unresolved_count(self) -> int:
        """How many escalations still wait for a person, however old."""
        stmt = (
            select(func.count())
            .select_from(DecisionRow)
            .where(DecisionRow.outcome == "escalate", DecisionRow.resolved_at.is_(None))
        )
        with self.dstore.read() as session:
            return int(session.scalar(stmt) or 0)

    def used_today(self, day_start: float) -> dict[str, int]:
        """``grant id -> how many acts it allowed`` since ``day_start``
        (the start of the cap day, as the usage pool's ``day()`` gives
        it): what :func:`decide` takes as ``used_today``."""
        stmt = (
            select(DecisionRow.grant_id, func.count())
            .where(
                DecisionRow.outcome == "allow",
                DecisionRow.grant_id.is_not(None),
                DecisionRow.at >= day_start,
            )
            .group_by(DecisionRow.grant_id)
        )
        with self.dstore.read() as session:
            return {str(grant_id): int(count) for grant_id, count in session.execute(stmt)}

    def resolve(
        self, decision_id: str, *, by: str | None, resolution: Resolution, now: float
    ) -> DecisionRecord | None:
        """Close an escalation. ``None`` when there is no such decision or
        it is not an escalation; one already resolved is returned as it
        was — the first resolution stands."""
        with self.dstore.transaction() as session:
            row = session.get(DecisionRow, decision_id)
            if row is None or row.outcome != "escalate":
                return None
            if row.resolved_at is None:
                row.resolved_at = now
                row.resolved_by = by
                row.resolution = resolution
                session.flush()
            return _decision(row)
