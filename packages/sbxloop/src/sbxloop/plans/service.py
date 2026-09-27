"""The rules every change to a plan goes through.

Every surface (the API today; the planner and chat later) creates and edits
plans here, so the rules hold once: a node breaks down one level at a time
(initiative → epic → task), a task lives in its epic's repository, a
dependency names a sibling task and never makes a cycle, a repository
whose forge cannot hold a plan is refused by name, a published node is not
edited here, and every mutation names the revision it read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from sbxloop.config import Config
from sbxloop.daemon.controls.principal import WORKSPACE_ID
from sbxloop.plans.hierarchy import repository_planning
from sbxloop.plans.model import Level, Plan, PlanNode, child_level
from sbxloop.plans.store import PlanEvent, PlanGone, PlanStore, StaleRevision, new_id

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

    # -- the rules ------------------------------------------------------------

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
        planning = repository_planning(str(config.vcs_kind_for(entry.repo)))
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


def _deleted(plan_id: str) -> dict[str, Any]:
    return {"plan_id": plan_id, "node_id": None, "change": "deleted"}
