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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from sqlalchemy import and_, delete, func, or_, select

from lantern.daemon.controls.delegation import Conditions, Decision, DecisionOutcome, Grant
from lantern.daemon.store import DaemonStore
from lantern.db.daemon_models import DecisionRow, GrantRow
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
