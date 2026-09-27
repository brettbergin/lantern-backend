"""The rules every change to a plan goes through.

Every surface (the API today; the planner and chat later) creates and edits
plans here, so the rules hold once: a node breaks down one level at a time
(initiative → epic → task), a task lives in its epic's repository, a
dependency names a sibling task and never makes a cycle, a repository
whose forge cannot hold a plan is refused by name, a published node is not
edited here, and every mutation names the revision it read. After publish
the forge wins: :meth:`PlanService.reconcile` folds it in (#2342).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from sbxloop.config import Config
from sbxloop.daemon.controls.principal import WORKSPACE_ID
from sbxloop.errors import SbxloopError
from sbxloop.log import get_logger
from sbxloop.plans.hierarchy import FORGE_NAMES, repository_planning_for
from sbxloop.plans.model import Level, Plan, PlanNode, child_level
from sbxloop.plans.publish import LevelResult, level_targets, publish_level
from sbxloop.plans.reconcile import Reconciliation, reconcile_plan
from sbxloop.plans.store import (
    PlanEvent,
    PlanGone,
    PlanStore,
    Reconciled,
    StaleRevision,
    new_id,
)
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
                "this node is on the forge; edit its issue there",
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
            if plan.id in self._publishing:
                raise PlanRefusal(
                    409, "already_in_progress", "this plan is being published right now"
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
            busy = plan.id in self._publishing or plan.id in self._reconciling
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


def _node_changed(plan_id: str, node_id: str, change: str) -> PlanEvent:
    return PlanEvent(
        "plan.node.changed", {"plan_id": plan_id, "node_id": node_id, "change": change}
    )


def _title(plan: Plan, node_id: str) -> str:
    node = plan.node(node_id)
    return node.title if node is not None else node_id


def _deleted(plan_id: str) -> dict[str, Any]:
    return {"plan_id": plan_id, "node_id": None, "change": "deleted"}
