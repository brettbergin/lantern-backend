"""The rules every change to a plan goes through.

Every surface (the API today; the planner and chat later) creates and edits
plans here, so the rules hold once: a node breaks down one level at a time
(initiative → epic → task), a task lives in its epic's repository, a
dependency names a sibling task and never makes a cycle, a repository
whose forge cannot hold a plan is refused by name, and every mutation
names the revision it read. After publish the forge wins:
:meth:`PlanService.reconcile` folds it in (#2342), and the only writes to a
published node are a person's direct ones — :meth:`PlanService.edit_published`,
:meth:`~PlanService.attach` and :meth:`~PlanService.detach` (#2350) — each
written to the forge at once and never over a forge change.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from sbxloop.api.publicids import run_public_id
from sbxloop.config import Config
from sbxloop.daemon.controls.principal import WORKSPACE_ID
from sbxloop.engine.planning import PlanBrief, PlanProposal, ProfileRef
from sbxloop.errors import SbxloopError
from sbxloop.log import get_logger
from sbxloop.plans.direct import (
    Linked,
    LinkRefused,
    add_level_label,
    gone,
    issue_write,
    labelled,
    link_child,
    say,
    unlink_child,
    without_dependency,
)
from sbxloop.plans.hierarchy import FORGE_NAMES, repository_planning_for
from sbxloop.plans.model import (
    CONTENT_FIELDS,
    ForgeRef,
    Level,
    Plan,
    PlanNode,
    child_level,
    content,
    content_version,
)
from sbxloop.plans.publish import LevelResult, level_targets, publish_level
from sbxloop.plans.reconcile import (
    Reconciliation,
    Seen,
    as_forge_has_it,
    key_of,
    reconcile_plan,
    seen_of,
)
from sbxloop.plans.render import drop_reference, markers, parse_issue_url
from sbxloop.plans.store import (
    PlanEvent,
    PlanGone,
    PlanStore,
    Reconciled,
    StaleRevision,
    new_id,
)
from sbxloop.vcs.checklist import ChecklistMangled
from sbxloop.vcs.protocol import IssueOps

#: The sections a person may set on a node, as the API and the store name
#: them. ``title`` is required on create.
SECTIONS: tuple[str, ...] = (
    "title",
    "goal",
    "context",
    "acceptance_criteria",
    "kind",
    "workload_profile",
    "verify_commands",
    "depends_on",
    "non_goals",
    "constraints",
)
#: Sections only a task carries.
TASK_SECTIONS = frozenset({"kind", "workload_profile", "verify_commands", "depends_on"})
#: Why opening a plan did not read the forge although it was due: nothing
#: has needed the forge sandbox yet, and a read never boots it.
IDLE = "the forge connection is not up yet; a sync reads the forge now"
NO_FORGE = "the daemon has no forge connection"

log = get_logger(__name__)


class PlanRefusal(Exception):
    """A change the plan service will not make, as a route renders it."""

    def __init__(self, status: int, code: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra


def _not_found(plan_id: str) -> PlanRefusal:
    return PlanRefusal(404, "not_found", f"no plan {plan_id}")


def _stale(exc: StaleRevision) -> PlanRefusal:
    return PlanRefusal(
        409,
        "stale_revision",
        f"the plan changed since it was read; it is at revision {exc.current}",
        current_revision=exc.current,
    )


@dataclass(frozen=True, slots=True)
class Attached:
    """An attach's outcome: the plan as it now is, the node that follows
    the issue, how it is linked under its parent, and why a native link
    became a checklist line."""

    plan: Plan
    node_id: str
    linked: Linked
    reason: str | None = None


class PlanService:
    def __init__(self, store: PlanStore, config: Callable[[], Config]) -> None:
        self.store = store
        self._config = config
        # Plans being published now: two walks of one plan at once would
        # both miss the other's issues and create them twice.
        self._publishing: set[str] = set()
        self._publishing_lock = threading.Lock()
        # Plans being reconciled now, and when each was last attempted (a
        # forge that is down is not asked again on every open).
        self._reconciling: set[str] = set()
        self._attempted: dict[str, float] = {}
        # Plans a person's direct write is on the forge for now: a
        # reconcile reading the forge meanwhile would fold the half-written
        # state in as someone else's edit.
        self._writing: set[str] = set()

    # -- reads ----------------------------------------------------------------

    def get(self, plan_id: str) -> Plan:
        plan = self.store.get(plan_id)
        if plan is None:
            raise _not_found(plan_id)
        return plan

    def find(
        self,
        *,
        repository: str | None = None,
        level: str | None = None,
        state: str | None = None,
    ) -> list[Plan]:
        """Every plan in the workspace, drafts included, most recently
        changed first; ``repository`` matches the plan's home or any node's
        repository, ``level`` and ``state`` the plan's own."""
        out: list[Plan] = []
        wanted = repository.casefold() if repository else None
        for plan in self.store.all():
            if level and plan.root.level != level:
                continue
            if state and plan.state != state:
                continue
            if wanted and not any(node.repository.casefold() == wanted for node in plan.nodes):
                continue
            out.append(plan)
        return out

    # -- writes ---------------------------------------------------------------

    def create(
        self,
        *,
        level: str,
        repository: str,
        sections: Mapping[str, Any],
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """A new draft plan whose root is an initiative or a lone epic."""
        if level not in ("initiative", "epic"):
            raise PlanRefusal(422, "invalid_argument", "a plan starts at an initiative or an epic")
        repo = self._repository(repository)
        plan_id = new_id("plan_")
        root = PlanNode(
            id=new_id("node_"),
            plan_id=plan_id,
            parent_id=None,
            position=0,
            level=level,  # type: ignore[arg-type]
            repository=repo,
            state="draft",
            origin="person",
            title="",
            created_at=now,
            updated_at=now,
        )
        root = self._with_sections(root, sections, siblings=[])
        plan = Plan(
            id=plan_id,
            workspace_id=WORKSPACE_ID,
            root_id=root.id,
            archived=False,
            created_by=str(actor.get("id") or "") or None,
            created_by_display=str(actor.get("display") or "") or None,
            created_at=now,
            updated_at=now,
            revision=1,
            nodes=(root,),
        )
        return self.store.create(
            plan,
            events=[
                PlanEvent(
                    "plan.created",
                    {"plan_id": plan_id, "level": level, "repository": repo},
                )
            ],
            actor=dict(actor),
        )

    def update(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        sections: Mapping[str, Any],
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """Edit the plan's own sections: its root node's."""
        plan = self.get(plan_id)
        return self.update_node(
            plan_id,
            plan.root_id,
            expected_revision=expected_revision,
            sections=sections,
            position=None,
            now=now,
            actor=actor,
        )

    def add_node(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        parent_id: str,
        repository: str | None,
        sections: Mapping[str, Any],
        position: int | None,
        now: float,
        actor: Mapping[str, Any],
    ) -> tuple[Plan, str]:
        """A new child under ``parent_id``, one level down; the plan and the
        new node's id."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        parent = self._node(plan, parent_id)
        level = child_level(parent.level)
        if level is None:
            raise PlanRefusal(422, "invalid_argument", "a task has no children")
        repo = self._child_repository(parent, level, repository)
        siblings = plan.children(parent.id)
        node = PlanNode(
            id=new_id("node_"),
            plan_id=plan.id,
            parent_id=parent.id,
            position=len(siblings),
            level=level,
            repository=repo,
            state="draft",
            origin="person",
            title="",
            created_at=now,
            updated_at=now,
        )
        node = self._with_sections(node, sections, siblings=siblings)
        ordered = self._reorder([*siblings, node], node.id, position)
        changed = self._write(
            plan,
            expected_revision,
            now,
            upsert=ordered,
            events=[_node_changed(plan.id, node.id, "added")],
            actor=actor,
        )
        return changed, node.id

    def update_node(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        sections: Mapping[str, Any],
        position: int | None,
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """Edit a node's sections or move it among its siblings. Editing a
        proposed or approved node makes it a draft again."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        if node.state == "published" and sections:
            raise PlanRefusal(
                409,
                "node_published",
                "this node is on the forge: an edit of it writes its issue (edit_published)",
                node_id=node.id,
            )
        upsert: dict[str, PlanNode] = {}
        edited = node
        if sections:
            siblings = plan.children(node.parent_id) if node.parent_id else []
            edited = self._with_sections(
                node, sections, siblings=[s for s in siblings if s.id != node.id]
            )
            if edited.state in ("proposed", "approved"):
                edited = replace(edited, state="draft")
            edited = replace(edited, updated_at=now)
            upsert[edited.id] = edited
        if position is not None:
            if node.parent_id is None:
                raise PlanRefusal(422, "invalid_argument", "the plan's root has no siblings")
            siblings = [edited if s.id == node.id else s for s in plan.children(node.parent_id)]
            for moved in self._reorder(siblings, node.id, position):
                upsert[moved.id] = moved
        if not upsert:
            raise PlanRefusal(422, "invalid_argument", "nothing to change")
        return self._write(
            plan,
            expected_revision,
            now,
            upsert=list(upsert.values()),
            events=[_node_changed(plan.id, node.id, "updated")],
            actor=actor,
        )

    def remove_node(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """Remove an unpublished node and everything under it; its siblings
        stop depending on it."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        if node.parent_id is None:
            raise PlanRefusal(
                422, "invalid_argument", "the root is the plan: delete the plan instead"
            )
        gone = [node, *plan.descendants(node.id)]
        if any(n.state == "published" for n in gone):
            raise PlanRefusal(
                409,
                "node_published",
                "this node, or one under it, is on the forge; detach it instead",
                node_id=node.id,
            )
        removed = {n.id for n in gone}
        siblings = [s for s in plan.children(node.parent_id) if s.id not in removed]
        upsert = [
            replace(
                s,
                position=index,
                depends_on=tuple(d for d in s.depends_on if d not in removed),
            )
            for index, s in enumerate(siblings)
        ]
        return self._write(
            plan,
            expected_revision,
            now,
            upsert=upsert,
            remove=sorted(removed),
            events=[_node_changed(plan.id, node.id, "removed")],
            actor=actor,
        )

    def delete(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        now: float,
        actor: Mapping[str, Any],
    ) -> str:
        """Delete a draft plan, or archive one with anything on the forge
        (the issues stay; sbxloop stops offering the plan). ``deleted`` or
        ``archived``."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        if plan.state == "draft":
            try:
                self.store.delete(
                    plan.id,
                    expected_revision=expected_revision,
                    now=now,
                    events=[PlanEvent("plan.node.changed", _deleted(plan.id))],
                    actor=dict(actor),
                )
            except StaleRevision as exc:
                raise _stale(exc) from exc
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            return "deleted"
        if not plan.archived:
            self._write(
                plan,
                expected_revision,
                now,
                archived=True,
                events=[_node_changed(plan.id, plan.root_id, "archived")],
                actor=actor,
            )
        return "archived"

    def approve(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        node_ids: Sequence[str] | None,
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """A person's "this is right": ``node_id``'s draft and proposed
        children — all of them, or the ones named — become ``approved``,
        ready to publish."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        children = plan.children(node.id)
        chosen = children
        if node_ids is not None:
            wanted = set(node_ids)
            unknown = sorted(wanted - {c.id for c in children})
            if unknown:
                raise PlanRefusal(
                    422,
                    "invalid_argument",
                    f"not children of {node.id}: {', '.join(unknown)}",
                    node_ids=unknown,
                )
            chosen = [c for c in children if c.id in wanted]
        approved = [
            replace(c, state="approved", updated_at=now)
            for c in chosen
            if c.state in ("draft", "proposed")
        ]
        if not approved:
            raise PlanRefusal(
                422,
                "invalid_argument",
                f"nothing to approve: no draft or proposed children of {node.id}",
            )
        return self._write(
            plan,
            expected_revision,
            now,
            upsert=approved,
            events=[
                PlanEvent(
                    "plan.node.changed",
                    {
                        "plan_id": plan.id,
                        "node_id": node.id,
                        "change": "approved",
                        "node_ids": [c.id for c in approved],
                    },
                )
            ],
            actor=actor,
        )

    def publish(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        forge_kind: str | None,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any],
    ) -> LevelResult:
        """Publish ``node_id``'s level: its approved children, and the node
        first when it is not on the forge yet (see
        :mod:`~sbxloop.plans.publish`). Everything that would refuse the
        level is checked before the forge is touched; ``forge_kind`` is the
        forge the daemon's connection speaks (``None``: it has none), and
        ``connect`` opens it. Records ``plan.published`` with the result."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        self._check_level(plan, node)
        targets = level_targets(plan, node.id)
        repos = list(dict.fromkeys([node.repository, *(t.repository for t in targets)]))
        for repo in repos:
            self._check_publishable(repo, forge_kind)
        self._check_cap(plan, node, targets)
        self._check_dependencies_published(plan, node, targets)
        if forge_kind is None:
            raise PlanRefusal(503, "source_unavailable", "the daemon has no forge connection")
        with self._publishing_lock:
            if plan.id in self._publishing or plan.id in self._writing:
                raise PlanRefusal(
                    409, "already_in_progress", "this plan is being written to the forge right now"
                )
            self._publishing.add(plan.id)
        try:
            try:
                ops = connect()
            except SbxloopError as exc:
                raise PlanRefusal(
                    503, "source_unavailable", f"could not reach the forge: {exc}"
                ) from exc
            result = publish_level(
                ops,
                store=self.store,
                config=self._config(),
                plan=plan,
                node_id=node.id,
                clock=clock,
                actor=actor,
            )
            self.store.note(
                plan.id,
                now=clock(),
                events=[
                    PlanEvent(
                        "plan.published",
                        {
                            "plan_id": plan.id,
                            "node_id": node.id,
                            "published": result.published,
                            "failed": result.failed,
                        },
                    )
                ],
                actor=dict(actor),
            )
            return result
        finally:
            with self._publishing_lock:
                self._publishing.discard(plan.id)

    # -- the forge wins ---------------------------------------------------------

    def open(
        self,
        plan_id: str,
        *,
        forge_kind: str | None,
        ready: bool,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any] | None,
    ) -> Reconciliation:
        """The plan as a client opens it: reconciled first when it has
        anything on the forge and its last reading is older than
        ``[planning] reconcile_interval_s``. Never fails for the forge: a
        forge that is down, or a reading that goes wrong, serves the stored
        plan with the reason."""
        plan = self.get(plan_id)
        try:
            return self.reconcile(
                plan_id,
                forge_kind=forge_kind,
                connect=connect,
                clock=clock,
                actor=actor,
                force=False,
                ready=ready,
            )
        except PlanRefusal as exc:
            if exc.status == 404:
                raise
            return Reconciliation(plan, 0, exc.detail)
        except Exception as exc:  # a read never fails for the forge
            log.warning("plans.reconcile_failed", plan_id=plan_id, error=repr(exc))
            return Reconciliation(plan, 0, f"could not read the forge: {type(exc).__name__}: {exc}")

    def reconcile(
        self,
        plan_id: str,
        *,
        forge_kind: str | None,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any] | None,
        force: bool,
        ready: bool = True,
    ) -> Reconciliation:
        """Read the plan's tree on the forge and fold it in (see
        :mod:`~sbxloop.plans.reconcile`); never writes to the forge.
        ``force`` is a sync a person asked for: it skips the interval and
        refuses what an open would quietly skip (an archived plan, a plan
        being published or reconciled, no forge)."""
        plan = self.get(plan_id)
        stored = Reconciliation(plan, 0, plan.reconcile_error)
        if plan.archived:
            if force:
                raise PlanRefusal(409, "plan_archived", "this plan is archived")
            return stored
        if not plan.root.followed:
            # Nothing on the forge, or a root whose issue is gone: there is
            # no tree to read from.
            return stored
        if not force:
            interval = self._config().planning_for(plan.root.repository).reconcile_interval_s
            last = max(self._attempted.get(plan.id, 0.0), plan.reconciled_at or 0.0)
            if interval <= 0 or clock() - last < interval:
                return stored
            if not ready:
                return Reconciliation(plan, 0, IDLE)
        with self._publishing_lock:
            busy = (
                plan.id in self._publishing
                or plan.id in self._reconciling
                or plan.id in self._writing
            )
            if busy:
                if force:
                    raise PlanRefusal(
                        409,
                        "already_in_progress",
                        "this plan is being published or read from the forge right now",
                    )
                return stored
            self._reconciling.add(plan.id)
        try:
            self._attempted[plan.id] = clock()
            if forge_kind is None:
                return self._unreachable(plan, NO_FORGE, force)
            try:
                ops = connect()
            except SbxloopError as exc:
                return self._unreachable(plan, f"could not reach the forge: {exc}", force)
            try:
                return reconcile_plan(
                    ops,
                    store=self.store,
                    config=self._config(),
                    plan=plan,
                    clock=clock,
                    actor=actor,
                )
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
        finally:
            with self._publishing_lock:
                self._reconciling.discard(plan.id)

    def _unreachable(self, plan: Plan, reason: str, force: bool) -> Reconciliation:
        """The forge could not be read at all: said on the plan, which keeps
        its last reading; a sync is ``503``."""
        try:
            marked = self.store.mark_reconciled(plan.id, Reconciled(None, reason))
        except PlanGone as exc:
            raise _not_found(plan.id) from exc
        if force:
            raise PlanRefusal(503, "source_unavailable", reason)
        return Reconciliation(marked, 0, reason)

    def ack_drift(
        self,
        plan_id: str,
        *,
        expected_revision: int,
        node_ids: Sequence[str] | None,
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """Someone looked: the drift of every node — or of the nodes named —
        is marked seen, so the next forge edit is diffed against what they
        saw. Nothing to mark changes nothing."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        chosen = list(plan.nodes)
        if node_ids is not None:
            unknown = sorted(set(node_ids) - {n.id for n in plan.nodes})
            if unknown:
                raise PlanRefusal(
                    422,
                    "invalid_argument",
                    f"not nodes of {plan.id}: {', '.join(unknown)}",
                    node_ids=unknown,
                )
            wanted = set(node_ids)
            chosen = [n for n in plan.nodes if n.id in wanted]
        seen = [replace(n, drift=()) for n in chosen if n.drift]
        if not seen:
            return plan
        return self._write(
            plan,
            expected_revision,
            now,
            upsert=seen,
            events=[
                PlanEvent(
                    "plan.node.changed",
                    {
                        "plan_id": plan.id,
                        "node_id": None,
                        "change": "drift_seen",
                        "node_ids": [n.id for n in seen],
                    },
                )
            ],
            actor=actor,
        )

    # -- the planner ------------------------------------------------------------

    def breakdown_target(
        self, plan_id: str, node_id: str, *, expected_revision: int | None = None
    ) -> tuple[Plan, PlanNode]:
        """The plan and the node a breakdown proposes the next level of,
        refused by name when the node cannot take one: a task has no
        children; a node on the forge that already has children is
        re-planned, not broken down again; a repository planning is off for
        (or no longer configured) cannot hold the level; a level at its cap
        has no room."""
        plan = self.get(plan_id)
        if expected_revision is not None:
            self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        level = child_level(node.level)
        if level is None:
            raise PlanRefusal(
                422, "invalid_argument", "a task has no children to propose", node_id=node.id
            )
        if node.state == "published" and plan.children(node.id):
            raise PlanRefusal(
                409,
                "replan_required",
                "this node is on the forge and already has children; re-plan it rather "
                "than breaking it down again",
                node_id=node.id,
            )
        config = self._config()
        if config.find_repo(node.repository) is None:
            raise PlanRefusal(
                409,
                "unknown_repository",
                f"{node.repository} is no longer a repository configured on this server",
                repository=node.repository,
            )
        planning = repository_planning_for(config, node.repository)
        if not planning.supported:
            raise PlanRefusal(
                409,
                "planning_unsupported",
                planning.reason or "this repository's forge can't hold plans",
                repository=node.repository,
            )
        cap = self._cap(node)
        kept = _kept(plan, node)
        if len(kept) >= cap:
            raise PlanRefusal(
                409,
                "level_full",
                f"this {node.level} already holds {len(kept)} of the {cap} "
                f"{_noun(level)} [planning] allows; remove one to propose more",
                node_id=node.id,
            )
        return plan, node

    def brief(self, plan_id: str, node_id: str, *, note: str = "") -> PlanBrief:
        """What a plan run is asked, read from the plan as it is now."""
        plan, node = self.breakdown_target(plan_id, node_id)
        level = child_level(node.level)
        assert level is not None  # nosec B101 - breakdown_target refused a task
        kept = _kept(plan, node)
        parent = plan.node(node.parent_id) if node.parent_id else None
        cap = self._cap(node)
        return PlanBrief(
            plan_id=plan.id,
            node_id=node.id,
            level=node.level,  # type: ignore[arg-type]
            child_level=level,  # type: ignore[arg-type]
            repository=node.repository,
            title=node.title,
            goal=node.goal,
            context=node.context,
            acceptance_criteria=list(node.acceptance_criteria),
            non_goals=node.non_goals,
            constraints=node.constraints,
            parent=(
                f"the {parent.level} “{parent.title}”"
                + (f" — {parent.goal.strip()}" if parent.goal.strip() else "")
                if parent is not None
                else ""
            ),
            kept=[child.title for child in kept],
            room=cap - len(kept),
            cap=cap,
            profiles=[
                ProfileRef(name=profile.name, description=profile.description or "")
                for profile in self._config().workloads
            ],
            repositories=sorted(
                {
                    child.repository
                    for child in kept
                    if child.repository.casefold() != node.repository.casefold()
                },
                key=str.casefold,
            ),
            note=note,
        )

    def deliver_proposal(
        self,
        plan_id: str,
        node_id: str,
        proposal: PlanProposal,
        *,
        run_id: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
    ) -> tuple[Plan, int]:
        """Write a plan run's proposal under its node: the node's previous
        ``proposed`` children (and anything under them) are replaced, the
        children a person made or approved stay where they are, and each
        proposed child is added as ``proposed`` with ``origin = planner``
        and its dependencies mapped to the new ids — one write, one
        revision, held to the same rules a person's edit is. The plan may
        have moved while the planner worked, so the write is made against
        the revision it reads, and read again when another write won."""
        for _ in range(3):
            plan, node = self.breakdown_target(plan_id, node_id)
            upsert, remove = self._proposed_children(plan, node, proposal, now)
            try:
                changed = self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=upsert,
                    remove=remove,
                    events=[
                        PlanEvent(
                            "plan.generation.proposed",
                            {
                                "plan_id": plan.id,
                                "node_id": node.id,
                                "run_id": run_public_id(run_id),
                                "count": len(proposal.children),
                            },
                            run_id=run_id,
                            item_id=item_id,
                            channel_id=channel_id,
                        )
                    ],
                    actor=dict(PLANNER),
                )
            except StaleRevision:
                continue
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            return changed, len(proposal.children)
        raise PlanRefusal(
            409, "stale_revision", "the plan kept changing while the proposal was written"
        )

    def generation_event(
        self,
        type_: str,
        plan_id: str,
        node_id: str,
        *,
        run_id: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
        **data: Any,
    ) -> None:
        """Record ``plan.generation.started`` or ``.failed`` for a run: a
        notice about the plan that changes none of it."""
        self.store.record(
            [
                PlanEvent(
                    type_,
                    {
                        "plan_id": plan_id,
                        "node_id": node_id,
                        "run_id": run_public_id(run_id),
                        **data,
                    },
                    run_id=run_id,
                    item_id=item_id,
                    channel_id=channel_id,
                )
            ],
            now=now,
            actor=dict(PLANNER),
        )

    def _cap(self, node: PlanNode) -> int:
        planning = self._config().planning_for(node.repository)
        if node.level == "initiative":
            return planning.max_epics_per_initiative
        return planning.max_tasks_per_epic

    def _proposed_children(
        self, plan: Plan, node: PlanNode, proposal: PlanProposal, now: float
    ) -> tuple[list[PlanNode], list[str]]:
        """The node upserts and removals that put ``proposal`` under ``node``."""
        level = child_level(node.level)
        assert level is not None  # nosec B101 - breakdown_target refused a task
        kept = _kept(plan, node)
        room = self._cap(node) - len(kept)
        if len(proposal.children) > room:
            raise PlanRefusal(
                409,
                "level_full",
                f"the {node.level} now has room for {max(room, 0)} more {_noun(level)}, "
                f"and {len(proposal.children)} were proposed",
                node_id=node.id,
            )
        replaced = [child for child in plan.children(node.id) if child.state == "proposed"]
        gone = [n for child in replaced for n in (child, *plan.descendants(child.id))]
        if any(n.state == "published" for n in gone):
            raise PlanRefusal(
                409,
                "node_published",
                "a proposed child has something on the forge under it; detach it first",
                node_id=node.id,
            )
        removed = {n.id for n in gone}
        stays = [
            replace(
                child,
                position=index,
                depends_on=tuple(d for d in child.depends_on if d not in removed),
            )
            for index, child in enumerate(kept)
        ]
        try:
            order = proposal.dependencies()
        except ValueError as exc:
            raise PlanRefusal(422, "invalid_argument", str(exc)) from exc
        repository = self._child_repository(node, level, None)
        fresh: list[PlanNode] = []
        for offset, child in enumerate(proposal.children):
            sections: dict[str, Any] = {
                "title": child.title,
                "goal": child.goal,
                "context": child.context,
                "acceptance_criteria": child.acceptance_criteria,
                "non_goals": child.non_goals,
                "constraints": child.constraints,
            }
            if level == "task":
                sections |= {
                    "kind": child.kind,
                    "workload_profile": child.workload_profile,
                    "verify_commands": child.verify_commands,
                }
            base = PlanNode(
                id=new_id("node_"),
                plan_id=plan.id,
                parent_id=node.id,
                position=len(stays) + offset,
                level=level,
                repository=repository,
                state="proposed",
                origin="planner",
                title="",
                created_at=now,
                updated_at=now,
            )
            fresh.append(self._with_sections(base, sections, siblings=[]))
        ids = [n.id for n in fresh]
        linked = [
            replace(n, depends_on=tuple(ids[d] for d in order[index]))
            for index, n in enumerate(fresh)
        ]
        for n in linked:
            if n.depends_on:
                self._check_dependencies(n, [*stays, *(o for o in linked if o.id != n.id)])
        return [*stays, *linked], sorted(removed)

    # -- a person's direct writes (#2350) ---------------------------------------

    def edit_published(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        forge_version: str | None,
        sections: Mapping[str, Any],
        position: int | None,
        forge_kind: str | None,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any],
    ) -> Plan:
        """Write a published node's title and sections to its issue at once
        (see :mod:`~sbxloop.plans.direct`) and record them. ``forge_version``
        is the version of the issue the client read — the node's
        ``forge.version``, or the one a ``forge_changed`` refusal answered
        with. The issue is read first and, when it changed on the forge
        since, the edit is ``409 forge_changed`` with the forge's current
        version and nothing is written. Neither forge offers a conditional
        update, so a forge edit landing between that read and the write is
        not seen: the window is one request long."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._followed(plan, node_id)
        if not sections:
            raise PlanRefusal(422, "invalid_argument", "nothing to change")
        if not forge_version:
            raise PlanRefusal(
                422,
                "invalid_argument",
                "an edit of a published node names the forge_version it read (its forge.version)",
            )
        if position is not None and node.parent_id is None:
            raise PlanRefusal(422, "invalid_argument", "the plan's root has no siblings")
        siblings = self._siblings(plan, node)
        self._check_published_dependencies(
            plan, node, self._with_sections(node, sections, siblings=siblings)
        )
        self._check_publishable(node.repository, forge_kind)
        assert node.forge is not None  # nosec B101 - _followed checks
        where = f"{node.repository}#{node.forge.number}"
        issue_url = node.forge.url
        with self._forge_write(plan.id, expected_revision) as plan:
            ops = self._connect(forge_kind, connect)
            seen = self._read(ops, node.repository, node.forge.number, missing=409)
            node = self._followed(plan, node_id)
            current = as_forge_has_it(plan, node, title=seen.title, body=seen.body)
            version = content_version(current)
            if version != forge_version:
                raise PlanRefusal(
                    409,
                    "forge_changed",
                    f"{where} changed on the forge since it was read; nothing was written. "
                    "Review its current version and edit again naming its forge_version",
                    forge_version=version,
                    current={
                        **content(current),
                        "forge_version": version,
                        "number": seen.number,
                        "url": seen.url or issue_url,
                    },
                )
            edited = self._with_sections(current, sections, siblings=self._siblings(plan, node))
            self._check_published_dependencies(plan, current, edited)
            title, body = issue_write(plan, current, edited, title=seen.title, body=seen.body)
            written: dict[str, Any] = {}
            if title is not None or body is not None:
                try:
                    written = ops.issue_update(node.repository, seen.number, title=title, body=body)
                except SbxloopError as exc:
                    raise PlanRefusal(
                        502, "forge_refused", f"could not write {where}: {say(exc)}"
                    ) from exc
            stamp = str(written.get("updated_at") or "") or seen.updated_at
            fields = [k for k in CONTENT_FIELDS if getattr(node, k) != getattr(edited, k)]

            def change(latest: Plan, now: float) -> list[PlanNode]:
                base = self._node(latest, node_id)
                assert base.forge is not None  # nosec B101 - followed above
                new = replace(
                    base,
                    **{k: getattr(edited, k) for k in CONTENT_FIELDS},
                    forge=replace(base.forge, updated_at=stamp),
                    updated_at=now,
                )
                if position is None or new.parent_id is None:
                    return [new]
                around = [new if s.id == new.id else s for s in latest.children(new.parent_id)]
                return self._reorder(around, new.id, position)

            return self._record(
                plan.id,
                change,
                event={
                    "node_id": node.id,
                    "change": "issue_edited",
                    "number": seen.number,
                    "fields": fields,
                    "wrote": [k for k, v in (("title", title), ("body", body)) if v is not None],
                },
                clock=clock,
                actor=actor,
            )

    def attach(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        repository: str | None,
        number: int | None,
        url: str | None,
        forge_kind: str | None,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any],
    ) -> Attached:
        """Link an existing open issue as a child one level under
        ``node_id``: a native sub-issue on GitHub, a line in the parent's
        managed checklist on GitLab, then its level label (never the trigger
        or the workload label). It is recorded ``published`` with ``origin =
        forge`` and its sections read from its body, the way a reconcile
        adopts one; a node of this plan that was detached from the same
        issue follows it again instead. Refused before the forge is written:
        a closed issue, a pull request, one already in this plan or in
        another, a task outside its epic's repository, a parent at its cap —
        and on GitHub one already under another parent."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        parent = self._followed(plan, node_id)
        level = child_level(parent.level)
        if level is None:
            raise PlanRefusal(422, "invalid_argument", "a task has no children")
        asked, wanted = self._issue_named(repository, number, url)
        repo = self._child_repository(parent, level, asked)
        for name in dict.fromkeys([parent.repository, repo]):
            self._check_publishable(name, forge_kind)
        existing = self._by_issue(plan, repo, wanted)
        if existing is not None and (existing.followed or existing.level != level):
            raise PlanRefusal(
                409,
                "already_in_plan",
                f"{repo}#{wanted} is already in this plan as {existing.title}",
                node_id=existing.id,
            )
        self._check_room(plan, parent)
        assert parent.forge is not None  # nosec B101 - _followed checks
        where = f"{repo}#{wanted}"
        with self._forge_write(plan.id, expected_revision) as plan:
            ops = self._connect(forge_kind, connect)
            try:
                row = ops.issue_get(repo, wanted)
            except SbxloopError as exc:
                if gone(exc):
                    raise PlanRefusal(
                        404, "issue_not_found", f"there is no issue {where}", repository=repo
                    ) from exc
                raise PlanRefusal(
                    502, "forge_refused", f"could not read {where}: {say(exc)}"
                ) from exc
            if "pull_request" in row:
                raise PlanRefusal(422, "not_an_issue", f"{where} is a pull request, not an issue")
            seen = seen_of(row, repo, wanted)
            if seen.state == "closed":
                raise PlanRefusal(409, "issue_closed", f"{where} is closed: attach an open issue")
            for other_plan, other_node in markers(seen.body):
                if other_plan != plan.id:
                    raise PlanRefusal(
                        409,
                        "in_another_plan",
                        f"{where} belongs to another plan ({other_plan})",
                        plan_id=other_plan,
                    )
                known = plan.node(other_node)
                if known is not None and (existing is None or known.id != existing.id):
                    raise PlanRefusal(
                        409,
                        "already_in_plan",
                        f"{where} is already in this plan as {known.title}",
                        node_id=known.id,
                    )
            title = (" ".join(seen.title.split()) or where)[:256]
            try:
                linked, reason = link_child(ops, self._config(), parent, repo, wanted, title)
                label = self._config().labels_for(repo).levels.get(level)
                if not label or not labelled(row, label):
                    add_level_label(ops, self._config(), repo, wanted, level)
            except LinkRefused as exc:
                raise PlanRefusal(409, exc.code, exc.detail, repository=repo) from exc
            except ChecklistMangled as exc:
                raise PlanRefusal(
                    409,
                    "checklist_mangled",
                    f"the managed checklist of {parent.repository}#{parent.forge.number} "
                    f"cannot be written: {exc}",
                ) from exc
            except SbxloopError as exc:
                raise PlanRefusal(
                    502, "forge_refused", f"could not attach {where}: {say(exc)}"
                ) from exc
            ref = ForgeRef(
                number=seen.number,
                url=seen.url,
                state=seen.state,
                updated_at=seen.updated_at,
            )
            child_id = existing.id if existing is not None else new_id("node_")

            def change(latest: Plan, now: float) -> list[PlanNode]:
                position = max((c.position for c in latest.children(parent.id)), default=-1) + 1
                again = latest.node(child_id)
                if again is not None and again.forge is not None:
                    return [
                        replace(
                            again,
                            parent_id=parent.id,
                            position=position,
                            forge=replace(again.forge, detached=None),
                            updated_at=now,
                        )
                    ]
                blank = PlanNode(
                    id=child_id,
                    plan_id=latest.id,
                    parent_id=parent.id,
                    position=position,
                    level=level,
                    repository=repo,
                    state="published",
                    origin="forge",
                    title=title,
                    forge=ref,
                    created_at=now,
                    updated_at=now,
                )
                adopted = as_forge_has_it(latest, blank, title=title, body=seen.body)
                return [replace(adopted, title=title)]

            changed = self._record(
                plan.id,
                change,
                event={
                    "node_id": child_id,
                    "change": "attached",
                    "parent_id": parent.id,
                    "number": seen.number,
                    "url": seen.url,
                    "linked": linked,
                    "reattached": existing is not None,
                },
                clock=clock,
                actor=actor,
            )
            return Attached(changed, child_id, linked, reason)

    def detach(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        forge_kind: str | None,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any],
    ) -> Plan:
        """Unlink a published child from its parent on the forge — its
        sub-issue link, any checklist line — without closing its issue. The
        node stays in the plan *detached*, as a reconcile leaves a node its
        parent no longer lists: ``forge.detached`` says a person did it,
        it is not followed any more, its subtree is left as it was, and
        linking the issue again (here or on the forge) makes it followed
        again. Its siblings stop depending on it, in the plan and in their
        issues' ``Depends on`` lists (only the items naming it go)."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        parent_id = self._node(plan, node_id).parent_id
        if parent_id is None:
            raise PlanRefusal(
                422, "invalid_argument", "the root has no parent: archive the plan instead"
            )
        node = self._followed(plan, node_id)
        parent = self._node(plan, parent_id)
        if parent.state != "published" or parent.forge is None:
            raise PlanRefusal(409, "parent_unpublished", f"{parent.title} is not on the forge")
        self._check_publishable(parent.repository, forge_kind)
        assert node.forge is not None  # nosec B101 - _followed checks
        where = f"{parent.repository}#{parent.forge.number}"
        with self._forge_write(plan.id, expected_revision) as plan:
            ops = self._connect(forge_kind, connect)
            try:
                rewritten = self._drop_dependents(ops, plan, node)
                unlinked = unlink_child(ops, self._config(), parent, node)
            except ChecklistMangled as exc:
                raise PlanRefusal(
                    409,
                    "checklist_mangled",
                    f"the managed checklist of {where} cannot be written: {exc}",
                ) from exc
            except SbxloopError as exc:
                raise PlanRefusal(
                    502,
                    "forge_refused",
                    f"could not unlink {node.repository}#{node.forge.number} from {where}: "
                    f"{say(exc)}",
                ) from exc
            who = str(actor.get("display") or actor.get("id") or "someone")
            reason = f"{who} detached it from {where} in the app; its issue stays open"

            def change(latest: Plan, now: float) -> list[PlanNode]:
                base = self._node(latest, node_id)
                assert base.forge is not None  # nosec B101 - followed above
                siblings = [s for s in latest.children(base.parent_id or "") if s.id != base.id]
                return [
                    replace(base, forge=replace(base.forge, detached=reason), updated_at=now),
                    *without_dependency(siblings, base.id),
                ]

            return self._record(
                plan.id,
                change,
                event={
                    "node_id": node.id,
                    "change": "detached",
                    "parent_id": parent.id,
                    "number": node.forge.number,
                    "unlinked": list(unlinked),
                    "dependents": rewritten,
                },
                clock=clock,
                actor=actor,
            )

    def _drop_dependents(self, ops: IssueOps, plan: Plan, node: PlanNode) -> list[str]:
        """Before ``node`` is unlinked, its siblings that depend on it stop
        saying so on the forge too — the ``Depends on`` list is the source
        of truth — by removing only the items that name its issue; the ids
        of the siblings whose issue was rewritten. One whose issue is gone
        is left to a reconcile."""
        assert node.forge is not None  # nosec B101 - the caller checks
        rewritten: list[str] = []
        for sibling in self._siblings(plan, node):
            if node.id not in sibling.depends_on or not sibling.followed:
                continue
            assert sibling.forge is not None  # nosec B101 - followed
            try:
                row = ops.issue_get(sibling.repository, sibling.forge.number)
            except SbxloopError as exc:
                if gone(exc):
                    continue
                raise
            body = str(row.get("body") or "")
            dropped = drop_reference(body, sibling.repository, node.repository, node.forge.number)
            if dropped != body:
                ops.issue_update(sibling.repository, sibling.forge.number, body=dropped)
                rewritten.append(sibling.id)
        return rewritten

    @contextmanager
    def _forge_write(self, plan_id: str, expected_revision: int) -> Iterator[Plan]:
        """One direct write at a time per plan, never beside a publish or a
        reconcile of it; yields the plan as it is now, still at
        ``expected_revision``."""
        with self._publishing_lock:
            if (
                plan_id in self._publishing
                or plan_id in self._reconciling
                or (plan_id in self._writing)
            ):
                raise PlanRefusal(
                    409,
                    "already_in_progress",
                    "this plan is being written to or read from the forge right now",
                )
            self._writing.add(plan_id)
        try:
            plan = self.get(plan_id)
            self._check_revision(plan, expected_revision)
            yield plan
        finally:
            with self._publishing_lock:
                self._writing.discard(plan_id)

    @staticmethod
    def _connect(forge_kind: str | None, connect: Callable[[], IssueOps]) -> IssueOps:
        if forge_kind is None:
            raise PlanRefusal(503, "source_unavailable", NO_FORGE)
        try:
            return connect()
        except SbxloopError as exc:
            raise PlanRefusal(
                503, "source_unavailable", f"could not reach the forge: {exc}"
            ) from exc

    @staticmethod
    def _read(ops: IssueOps, repo: str, number: int, *, missing: int) -> Seen:
        try:
            row = ops.issue_get(repo, number)
        except SbxloopError as exc:
            if gone(exc):
                raise PlanRefusal(
                    missing,
                    "issue_gone",
                    f"{repo}#{number} is gone from the forge (or this server can no longer "
                    "see it); a sync detaches it",
                ) from exc
            raise PlanRefusal(
                502, "forge_refused", f"could not read {repo}#{number}: {say(exc)}"
            ) from exc
        return seen_of(row, repo, number)

    def _record(
        self,
        plan_id: str,
        change: Callable[[Plan, float], Sequence[PlanNode]],
        *,
        event: Mapping[str, Any],
        clock: Callable[[], float],
        actor: Mapping[str, Any],
    ) -> Plan:
        """Record what was just written to the forge, against the plan as
        it now is: the forge already has it, so another edit of the plan
        meanwhile is not a reason to lose it."""
        for _ in range(3):
            latest = self.get(plan_id)
            now = clock()
            try:
                return self.store.apply(
                    plan_id,
                    expected_revision=latest.revision,
                    now=now,
                    upsert=change(latest, now),
                    events=[PlanEvent("plan.node.changed", {"plan_id": plan_id, **event})],
                    actor=dict(actor),
                )
            except StaleRevision:
                continue
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
        raise PlanRefusal(
            409,
            "already_in_progress",
            "the forge was written but the plan kept changing while it was recorded; "
            "a sync reads it back",
        )

    @staticmethod
    def _followed(plan: Plan, node_id: str) -> PlanNode:
        """``node_id``, on the forge and still following its issue."""
        node = PlanService._node(plan, node_id)
        if node.state != "published" or node.forge is None:
            raise PlanRefusal(
                409,
                "node_unpublished",
                f"{node.title} is not on the forge yet",
                node_id=node.id,
            )
        if node.forge.detached:
            raise PlanRefusal(
                409,
                "node_detached",
                f"{node.title} no longer follows its issue ({node.forge.detached})",
                node_id=node.id,
            )
        return node

    @staticmethod
    def _siblings(plan: Plan, node: PlanNode) -> list[PlanNode]:
        if node.parent_id is None:
            return []
        return [s for s in plan.children(node.parent_id) if s.id != node.id]

    @staticmethod
    def _check_published_dependencies(plan: Plan, before: PlanNode, after: PlanNode) -> None:
        """A dependency an edit adds to a published node is a sibling that
        follows its issue: its ``Depends on`` section names it by
        reference."""
        missing = []
        for dep in after.depends_on:
            if dep in before.depends_on:
                continue
            sibling = plan.node(dep)
            if sibling is None or not sibling.followed:
                missing.append(dep)
        if missing:
            raise PlanRefusal(
                409,
                "dependency_unpublished",
                "a published task depends only on published siblings: "
                + ", ".join(_title(plan, d) for d in missing),
                node_ids=missing,
            )

    @staticmethod
    def _issue_named(
        repository: str | None, number: int | None, url: str | None
    ) -> tuple[str, int]:
        """The issue an attach names: ``repository`` and ``number``, or its
        web ``url``."""
        if url:
            parsed = parse_issue_url(url)
            if parsed is None:
                raise PlanRefusal(422, "invalid_argument", f"{url} is not an issue's web URL")
            if (repository and repository.casefold() != parsed[0].casefold()) or (
                number is not None and number != parsed[1]
            ):
                raise PlanRefusal(
                    422, "invalid_argument", "the url and the repository or number disagree"
                )
            return parsed
        if not repository or number is None:
            raise PlanRefusal(
                422, "invalid_argument", "name the issue: its repository and number, or its url"
            )
        return repository, number

    @staticmethod
    def _by_issue(plan: Plan, repo: str, number: int) -> PlanNode | None:
        key = key_of(repo, number)
        for node in plan.nodes:
            if node.forge is not None and key_of(node.repository, node.forge.number) == key:
                return node
        return None

    def _check_room(self, plan: Plan, parent: PlanNode) -> None:
        """One more child keeps ``parent`` within ``[planning]``'s cap."""
        planning = self._config().planning_for(parent.repository)
        cap, key = (
            (planning.max_epics_per_initiative, "max_epics_per_initiative")
            if parent.level == "initiative"
            else (planning.max_tasks_per_epic, "max_tasks_per_epic")
        )
        children = [c for c in plan.children(parent.id) if c.followed]
        if len(children) + 1 > cap:
            raise PlanRefusal(
                409,
                "too_many_children",
                f"{parent.title} already has {len(children)} children on the forge; "
                f"[planning] {key} is {cap}",
                cap=cap,
                children=len(children),
            )

    # -- the rules ------------------------------------------------------------

    @staticmethod
    def _check_level(plan: Plan, node: PlanNode) -> None:
        """A level publishes under a node that is on the forge, or is the
        plan's root; there must be something to write."""
        if node.level == "task":
            raise PlanRefusal(422, "invalid_argument", "a task has no children to publish")
        if node.state != "published" and node.parent_id is not None:
            parent = plan.node(node.parent_id)
            raise PlanRefusal(
                409,
                "parent_unpublished",
                f"{node.title} is not on the forge yet: publish "
                f"{parent.title if parent else node.parent_id}'s level first",
                parent_id=node.parent_id,
            )
        if node.state == "published" and not any(
            c.state == "approved" for c in plan.children(node.id)
        ):
            raise PlanRefusal(
                409,
                "nothing_to_publish",
                f"no approved children of {node.title} are waiting to be published",
            )

    def _check_publishable(self, repo: str, forge_kind: str | None) -> None:
        """``repo`` is configured, enabled, can hold a plan, and lives on
        the forge the daemon's connection speaks."""
        config = self._config()
        entry = config.find_repo(repo)
        if entry is None:
            raise PlanRefusal(
                422,
                "unknown_repository",
                f"{repo} is not a repository configured on this server",
                repository=repo,
            )
        if not entry.enabled:
            raise PlanRefusal(
                409,
                "repository_disabled",
                f"{entry.repo} is disabled on this server",
                repository=entry.repo,
            )
        planning = repository_planning_for(config, entry.repo)
        if not planning.supported:
            raise PlanRefusal(
                409,
                "planning_unsupported",
                planning.reason or "this repository's forge can't hold plans",
                repository=entry.repo,
            )
        kind = str(config.vcs_kind_for(entry.repo))
        if forge_kind is not None and kind != forge_kind:
            raise PlanRefusal(
                409,
                "forge_mismatch",
                f"{entry.repo} is on {FORGE_NAMES.get(kind, kind)}, but this server's forge "
                f"connection speaks {FORGE_NAMES.get(forge_kind, forge_kind)}",
                repository=entry.repo,
            )

    def _check_cap(self, plan: Plan, node: PlanNode, targets: Sequence[PlanNode]) -> None:
        """The level stays within ``[planning]``'s cap on one parent."""
        planning = self._config().planning_for(node.repository)
        cap, key = (
            (planning.max_epics_per_initiative, "max_epics_per_initiative")
            if node.level == "initiative"
            else (planning.max_tasks_per_epic, "max_tasks_per_epic")
        )
        going = {t.id for t in targets}
        children = [c for c in plan.children(node.id) if c.state == "published" or c.id in going]
        if len(children) > cap:
            raise PlanRefusal(
                409,
                "too_many_children",
                f"{node.title} would have {len(children)} children on the forge; "
                f"[planning] {key} is {cap}",
                cap=cap,
                children=len(children),
            )

    @staticmethod
    def _check_dependencies_published(
        plan: Plan, node: PlanNode, targets: Sequence[PlanNode]
    ) -> None:
        """Every dependency of a child being published is on the forge or
        published now, so its reference can be written."""
        going = {t.id for t in targets}
        missing: dict[str, list[str]] = {}
        for child in targets:
            if child.id == node.id:
                continue
            for dep in child.depends_on:
                sibling = plan.node(dep)
                if dep in going or (sibling is not None and sibling.state == "published"):
                    continue
                missing.setdefault(child.id, []).append(dep)
        if missing:
            named = "; ".join(
                f"{_title(plan, child)} depends on " + ", ".join(_title(plan, dep) for dep in deps)
                for child, deps in missing.items()
            )
            raise PlanRefusal(
                409,
                "dependency_unpublished",
                f"approve what these depend on, or leave them out: {named}",
                node_ids=sorted(missing),
            )

    def _repository(self, name: str) -> str:
        """The configured spelling of ``name``, refused when it is not a
        configured repository or its forge cannot hold a plan."""
        config = self._config()
        entry = config.find_repo(name)
        if entry is None:
            raise PlanRefusal(
                422,
                "unknown_repository",
                f"{name} is not a repository configured on this server",
                repository=name,
            )
        planning = repository_planning_for(config, entry.repo)
        if not planning.supported:
            raise PlanRefusal(
                409,
                "planning_unsupported",
                planning.reason or "this repository's forge can't hold plans",
                repository=entry.repo,
            )
        return entry.repo

    def _child_repository(self, parent: PlanNode, level: Level, asked: str | None) -> str:
        """An epic may target any plannable repository (its initiative's
        home by default); a task lives in its epic's."""
        if level == "task":
            if asked and asked.casefold() != parent.repository.casefold():
                raise PlanRefusal(
                    422,
                    "invalid_argument",
                    f"a task lives in its epic's repository, {parent.repository}",
                )
            return parent.repository
        return self._repository(asked) if asked else parent.repository

    def _with_sections(
        self, node: PlanNode, sections: Mapping[str, Any], *, siblings: Sequence[PlanNode]
    ) -> PlanNode:
        unknown = sorted(set(sections) - set(SECTIONS))
        if unknown:
            raise PlanRefusal(422, "invalid_argument", f"unknown sections: {unknown}")
        misplaced = sorted(k for k in sections if k in TASK_SECTIONS and sections[k] is not None)
        if misplaced and node.level != "task":
            raise PlanRefusal(
                422, "invalid_argument", f"only a task carries {', '.join(misplaced)}"
            )
        changes: dict[str, Any] = {}
        for key, value in sections.items():
            if key in ("acceptance_criteria", "verify_commands", "depends_on"):
                changes[key] = tuple(value or ())
            elif key in ("kind", "workload_profile"):
                changes[key] = value or None
            else:
                changes[key] = "" if value is None else str(value)
        edited = replace(node, **changes)
        if not edited.title.strip():
            raise PlanRefusal(422, "invalid_argument", "a node needs a title")
        if edited.kind != "workload" and edited.workload_profile:
            raise PlanRefusal(
                422, "invalid_argument", "only a workload task names a workload profile"
            )
        if edited.kind == "workload" and edited.verify_commands:
            raise PlanRefusal(422, "invalid_argument", "a workload task has no verify commands")
        if edited.workload_profile:
            profiles = {p.name for p in self._config().workloads}
            if edited.workload_profile not in profiles:
                raise PlanRefusal(
                    422,
                    "invalid_argument",
                    f"no workload profile {edited.workload_profile} is configured",
                )
        if "depends_on" in sections:
            self._check_dependencies(edited, siblings)
        return edited

    @staticmethod
    def _check_dependencies(node: PlanNode, siblings: Sequence[PlanNode]) -> None:
        by_id = {s.id: s for s in siblings}
        for dep in node.depends_on:
            if dep == node.id:
                raise PlanRefusal(422, "invalid_argument", "a task cannot depend on itself")
            if dep not in by_id:
                raise PlanRefusal(
                    422, "invalid_argument", f"{dep} is not a sibling task of this one"
                )
        # A cycle through the siblings back to this node.
        graph = {s.id: set(s.depends_on) for s in siblings}
        graph[node.id] = set(node.depends_on)
        seen: set[str] = set()
        stack = list(node.depends_on)
        while stack:
            current = stack.pop()
            if current == node.id:
                raise PlanRefusal(422, "invalid_argument", "those dependencies make a cycle")
            if current in seen:
                continue
            seen.add(current)
            stack.extend(graph.get(current, ()))

    @staticmethod
    def _reorder(
        siblings: Sequence[PlanNode], node_id: str, position: int | None
    ) -> list[PlanNode]:
        """``siblings`` renumbered 0..n-1, with ``node_id`` moved to
        ``position`` (clamped) when one is given."""
        ordered = sorted(siblings, key=lambda n: (n.position, n.id))
        if position is not None:
            moving = next(n for n in ordered if n.id == node_id)
            ordered = [n for n in ordered if n.id != node_id]
            ordered.insert(max(0, min(position, len(ordered))), moving)
        return [replace(n, position=index) for index, n in enumerate(ordered)]

    @staticmethod
    def _node(plan: Plan, node_id: str) -> PlanNode:
        node = plan.node(node_id)
        if node is None:
            raise PlanRefusal(404, "not_found", f"no node {node_id} in plan {plan.id}")
        return node

    @staticmethod
    def _check_revision(plan: Plan, expected: int) -> None:
        if plan.revision != expected:
            raise _stale(StaleRevision(plan.revision))

    @staticmethod
    def _not_archived(plan: Plan) -> None:
        if plan.archived:
            raise PlanRefusal(409, "plan_archived", "this plan is archived")

    def _write(
        self,
        plan: Plan,
        expected_revision: int,
        now: float,
        *,
        upsert: Sequence[PlanNode] = (),
        remove: Sequence[str] = (),
        archived: bool | None = None,
        events: Sequence[PlanEvent] = (),
        actor: Mapping[str, Any],
    ) -> Plan:
        try:
            return self.store.apply(
                plan.id,
                expected_revision=expected_revision,
                now=now,
                upsert=upsert,
                remove=remove,
                archived=archived,
                events=events,
                actor=dict(actor),
            )
        except StaleRevision as exc:
            raise _stale(exc) from exc
        except PlanGone as exc:
            raise _not_found(plan.id) from exc


#: Who a plan run's writes are attributed to.
PLANNER: Mapping[str, str] = {
    "kind": "system",
    "id": "planner",
    "display": "the planner",
    "via": "plan run",
}


def _kept(plan: Plan, node: PlanNode) -> list[PlanNode]:
    """The children of ``node`` a proposal leaves where they are: every one
    a person made, edited or approved — all but the planner's ``proposed``."""
    return [child for child in plan.children(node.id) if child.state != "proposed"]


def _noun(level: str) -> str:
    return "epics" if level == "epic" else "tasks"


def _node_changed(plan_id: str, node_id: str, change: str) -> PlanEvent:
    return PlanEvent(
        "plan.node.changed", {"plan_id": plan_id, "node_id": node_id, "change": change}
    )


def _title(plan: Plan, node_id: str) -> str:
    node = plan.node(node_id)
    return node.title if node is not None else node_id


def _deleted(plan_id: str) -> dict[str, Any]:
    return {"plan_id": plan_id, "node_id": None, "change": "deleted"}
