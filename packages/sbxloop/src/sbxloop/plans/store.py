"""Plans and their nodes in the daemon's store.

Reads return whole plans. Writes are one transaction each: the plan's
revision is checked against what the caller read, the node rows are
upserted or deleted, the revision is bumped, and the events that describe
the change land in the same transaction, so a client that sees the event
reads the change.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import delete, insert, select

from sbxloop.daemon.store import DaemonStore
from sbxloop.db.api_models import ApiEventRow
from sbxloop.db.daemon_models import PlanNodeRow, PlanRow
from sbxloop.engine.planning import Clarification
from sbxloop.plans.model import (
    Drift,
    DriftChange,
    ForgeRef,
    ForgeState,
    Level,
    NodeState,
    Origin,
    Plan,
    PlanNode,
    Replan,
    ReplanAction,
    ReplanEntry,
    TaskKind,
)

_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"


def new_id(prefix: str) -> str:
    return prefix + "".join(secrets.choice(_ALPHABET) for _ in range(16))


class StaleRevision(Exception):
    """The plan moved on since the caller read it."""

    def __init__(self, current: int) -> None:
        super().__init__(f"the plan is at revision {current}")
        self.current = current


class PlanGone(Exception):
    """The plan does not exist (or was deleted under the caller)."""


@dataclass(frozen=True, slots=True)
class PlanEvent:
    type: str
    data: dict[str, Any]
    #: The run, work item and channel the event belongs to, when a plan
    #: run caused it: a reader scoped to the run or its channel sees it.
    run_id: str | None = None
    item_id: str | None = None
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class Reconciled:
    """A forge read of a plan: ``at`` when it succeeded (``None`` when it
    could not happen at all, which keeps the last success), and what
    stopped it or part of it."""

    at: float | None
    error: str | None = None


def _stamp(row: PlanRow, reconciled: Reconciled) -> None:
    if reconciled.at is not None:
        row.reconciled_at = reconciled.at
    row.reconcile_error = reconciled.error


def _drift(raw: str | None) -> tuple[Drift, ...]:
    out: list[Drift] = []
    for entry in json.loads(raw or "[]"):
        if not isinstance(entry, dict):
            continue
        out.append(
            Drift(
                change=cast(DriftChange, entry.get("change")),
                at=float(entry.get("at") or 0.0),
                before=dict(entry.get("before") or {}),
                after=dict(entry.get("after") or {}),
                reason=entry.get("reason"),
            )
        )
    return tuple(out)


def _replan(raw: str | None) -> Replan | None:
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        return None
    entries = tuple(
        ReplanEntry(
            id=str(entry.get("id") or ""),
            action=cast(ReplanAction, entry.get("action")),
            node_id=str(entry.get("node_id") or ""),
            sections=dict(entry.get("sections") or {}),
            before=dict(entry.get("before") or {}),
            rationale=str(entry.get("rationale") or ""),
            error=entry.get("error"),
            forge_version=entry.get("forge_version"),
        )
        for entry in data.get("entries") or []
        if isinstance(entry, dict)
    )
    return Replan(
        id=str(data.get("id") or ""),
        run_id=data.get("run_id"),
        proposed_at=float(data.get("proposed_at") or 0.0),
        entries=entries,
    )


def _node(row: PlanNodeRow) -> PlanNode:
    forge = None
    if row.forge_number is not None:
        forge = ForgeRef(
            number=int(row.forge_number),
            url=str(row.forge_url or ""),
            state=cast(ForgeState | None, row.forge_state),
            updated_at=row.forge_updated_at,
            detached=row.forge_detached,
            marker_missing=bool(row.forge_marker_missing),
            checklist_error=row.forge_checklist_error,
        )
    return PlanNode(
        id=str(row.node_id),
        plan_id=str(row.plan_id),
        parent_id=row.parent_id,
        position=int(row.position),
        level=cast(Level, row.level),
        repository=str(row.repository),
        state=cast(NodeState, row.state),
        origin=cast(Origin, row.origin),
        title=str(row.title),
        goal=str(row.goal or ""),
        context=str(row.context or ""),
        acceptance_criteria=tuple(json.loads(row.acceptance_criteria_json or "[]")),
        kind=cast(TaskKind | None, row.kind),
        workload_profile=row.workload_profile,
        verify_commands=tuple(json.loads(row.verify_commands_json or "[]")),
        depends_on=tuple(json.loads(row.depends_on_json or "[]")),
        non_goals=str(row.non_goals or ""),
        constraints=str(row.constraints or ""),
        forge=forge,
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        drift=_drift(row.drift_json),
        generation=_generation(row.generation_json),
        replan=_replan(row.replan_json),
    )


def _generation(raw: str | None) -> Clarification | None:
    """A node's clarification as stored; one a later build wrote in a shape
    this one cannot read is treated as none, never as a crash of the plan."""
    if not raw:
        return None
    try:
        return Clarification.model_validate_json(raw)
    except ValueError:
        return None


def _columns(node: PlanNode) -> dict[str, Any]:
    return {
        "node_id": node.id,
        "plan_id": node.plan_id,
        "parent_id": node.parent_id,
        "position": node.position,
        "level": node.level,
        "repository": node.repository,
        "state": node.state,
        "origin": node.origin,
        "title": node.title,
        "goal": node.goal,
        "context": node.context,
        "acceptance_criteria_json": json.dumps(list(node.acceptance_criteria)),
        "kind": node.kind,
        "workload_profile": node.workload_profile,
        "verify_commands_json": json.dumps(list(node.verify_commands)),
        "depends_on_json": json.dumps(list(node.depends_on)),
        "non_goals": node.non_goals,
        "constraints": node.constraints,
        "forge_number": None if node.forge is None else node.forge.number,
        "forge_url": None if node.forge is None else node.forge.url,
        "forge_state": None if node.forge is None else node.forge.state,
        "forge_updated_at": None if node.forge is None else node.forge.updated_at,
        "forge_detached": None if node.forge is None else node.forge.detached,
        "forge_marker_missing": int(node.forge is not None and node.forge.marker_missing),
        "forge_checklist_error": None if node.forge is None else node.forge.checklist_error,
        "drift_json": json.dumps([d.as_dict() for d in node.drift], default=str),
        "replan_json": (
            None if node.replan is None else json.dumps(node.replan.as_dict(), default=str)
        ),
        "created_at": node.created_at,
        "updated_at": node.updated_at,
        "generation_json": (None if node.generation is None else node.generation.model_dump_json()),
    }


def _ordered(root_id: str, nodes: Iterable[PlanNode]) -> tuple[PlanNode, ...]:
    """The root first, then depth-first in each parent's order."""
    by_parent: dict[str | None, list[PlanNode]] = {}
    for node in nodes:
        by_parent.setdefault(node.parent_id, []).append(node)
    for siblings in by_parent.values():
        siblings.sort(key=lambda node: (node.position, node.id))
    out: list[PlanNode] = []

    def walk(node: PlanNode) -> None:
        out.append(node)
        for child in by_parent.get(node.id, []):
            walk(child)

    for node in by_parent.get(None, []):
        if node.id == root_id:
            walk(node)
    return tuple(out)


def _plan(row: PlanRow, nodes: Iterable[PlanNodeRow]) -> Plan:
    return Plan(
        id=str(row.plan_id),
        workspace_id=str(row.workspace_id),
        root_id=str(row.root_node_id),
        archived=row.state == "archived",
        created_by=row.created_by,
        created_by_display=row.created_by_display,
        created_at=float(row.created_at),
        updated_at=float(row.updated_at),
        revision=int(row.revision),
        nodes=_ordered(str(row.root_node_id), (_node(n) for n in nodes)),
        reconciled_at=None if row.reconciled_at is None else float(row.reconciled_at),
        reconcile_error=row.reconcile_error,
    )


def _event(session: Any, event: PlanEvent, now: float, actor: dict[str, Any] | None) -> None:
    session.execute(
        insert(ApiEventRow).values(
            recorded_at=now,
            occurred_at=now,
            type=event.type,
            run_id=event.run_id,
            item_id=event.item_id,
            operation_id=None,
            actor_json=None if actor is None else json.dumps(actor, default=str),
            source_seq=None,
            data_json=json.dumps(event.data, default=str),
            channel_id=event.channel_id,
            audience_user_id=None,
        )
    )


class PlanStore:
    def __init__(self, dstore: DaemonStore) -> None:
        self.dstore = dstore

    def get(self, plan_id: str) -> Plan | None:
        with self.dstore.read() as session:
            row = session.get(PlanRow, plan_id)
            if row is None:
                return None
            nodes = session.scalars(select(PlanNodeRow).where(PlanNodeRow.plan_id == plan_id))
            return _plan(row, nodes)

    def all(self) -> list[Plan]:
        """Every plan, most recently changed first."""
        with self.dstore.read() as session:
            rows = list(
                session.scalars(
                    select(PlanRow).order_by(PlanRow.updated_at.desc(), PlanRow.plan_id)
                )
            )
            nodes: dict[str, list[PlanNodeRow]] = {}
            for node in session.scalars(select(PlanNodeRow)):
                nodes.setdefault(str(node.plan_id), []).append(node)
            return [_plan(row, nodes.get(str(row.plan_id), [])) for row in rows]

    def create(
        self,
        plan: Plan,
        *,
        events: Sequence[PlanEvent],
        actor: dict[str, Any] | None,
    ) -> Plan:
        with self.dstore.transaction() as session:
            session.add(
                PlanRow(
                    plan_id=plan.id,
                    workspace_id=plan.workspace_id,
                    root_node_id=plan.root_id,
                    state="archived" if plan.archived else "active",
                    created_by=plan.created_by,
                    created_by_display=plan.created_by_display,
                    created_at=plan.created_at,
                    updated_at=plan.updated_at,
                    revision=plan.revision,
                )
            )
            for node in plan.nodes:
                session.add(PlanNodeRow(**_columns(node)))
            for event in events:
                _event(session, event, plan.created_at, actor)
        created = self.get(plan.id)
        assert created is not None  # nosec B101 - written above
        return created

    def apply(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        now: float,
        upsert: Sequence[PlanNode] = (),
        remove: Sequence[str] = (),
        archived: bool | None = None,
        events: Sequence[PlanEvent] = (),
        actor: dict[str, Any] | None = None,
        reconciled: Reconciled | None = None,
    ) -> Plan:
        """Write one change to a plan, against the revision the caller
        read; the plan as it now is. ``reconciled`` records the forge read
        the change came from, in the same transaction."""
        with self.dstore.transaction() as session:
            row = session.get(PlanRow, plan_id)
            if row is None:
                raise PlanGone(plan_id)
            if int(row.revision) != expected_revision:
                raise StaleRevision(int(row.revision))
            if remove:
                session.execute(
                    delete(PlanNodeRow).where(
                        PlanNodeRow.plan_id == plan_id, PlanNodeRow.node_id.in_(list(remove))
                    )
                )
            for node in upsert:
                existing = session.get(PlanNodeRow, node.id)
                columns = _columns(node)
                if existing is None:
                    session.add(PlanNodeRow(**columns))
                else:
                    for key, value in columns.items():
                        setattr(existing, key, value)
            if archived is not None:
                row.state = "archived" if archived else "active"
            if reconciled is not None:
                _stamp(row, reconciled)
            row.revision = int(row.revision) + 1
            row.updated_at = now
            for event in events:
                _event(session, event, now, actor)
        changed = self.get(plan_id)
        if changed is None:
            raise PlanGone(plan_id)
        return changed

    def mark_reconciled(self, plan_id: str, reconciled: Reconciled) -> Plan:
        """Record a forge read that changed nothing (or could not happen):
        when, and what stopped it. The revision stays where it is, so a
        client's ``expected_revision`` is not made stale by a read."""
        with self.dstore.transaction() as session:
            row = session.get(PlanRow, plan_id)
            if row is None:
                raise PlanGone(plan_id)
            _stamp(row, reconciled)
        plan = self.get(plan_id)
        if plan is None:
            raise PlanGone(plan_id)
        return plan

    def note(
        self,
        plan_id: str,
        *,
        now: float,
        events: Sequence[PlanEvent],
        actor: dict[str, Any] | None = None,
    ) -> None:
        """Record events about a plan that change none of its rows (a
        level's publish summary, after each node was written as it
        landed): the revision stays where it is."""
        with self.dstore.transaction() as session:
            if session.get(PlanRow, plan_id) is None:
                raise PlanGone(plan_id)
            for event in events:
                _event(session, event, now, actor)

    def record(
        self,
        events: Sequence[PlanEvent],
        *,
        now: float,
        actor: dict[str, Any] | None = None,
    ) -> None:
        """Record events that change no plan row — a generation starting or
        failing — without touching the plan's revision."""
        with self.dstore.transaction() as session:
            for event in events:
                _event(session, event, now, actor)

    def delete(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        now: float,
        events: Sequence[PlanEvent] = (),
        actor: dict[str, Any] | None = None,
    ) -> None:
        with self.dstore.transaction() as session:
            row = session.get(PlanRow, plan_id)
            if row is None:
                raise PlanGone(plan_id)
            if int(row.revision) != expected_revision:
                raise StaleRevision(int(row.revision))
            session.execute(delete(PlanNodeRow).where(PlanNodeRow.plan_id == plan_id))
            session.delete(row)
            for event in events:
                _event(session, event, now, actor)
