"""A person's drafting: creating a plan, adding, editing, moving and removing
nodes, approving a level — every write against the revision they read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from lantern.daemon.controls.principal import WORKSPACE_ID
from lantern.log import get_logger
from lantern.plans.model import (
    Plan,
    PlanNode,
    child_level,
    plain,
)
from lantern.plans.service_base import (
    SECTIONS,
    TASK_SECTIONS,
    PlanRefusal,
    _deleted,
    _node_changed,
    _not_found,
    _noun,
    _occupied,
    _ServiceBase,
    _stale,
)
from lantern.plans.store import (
    PlanEvent,
    PlanGone,
    StaleRevision,
    new_id,
)

log = get_logger(__name__)


class _Drafting(_ServiceBase):
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
        requested = self._with_sections(root, sections, siblings=[])
        brief = {
            key: plain(getattr(requested, key)) for key in SECTIONS if key not in TASK_SECTIONS
        }
        root = replace(root, title=f"Unplanned {level}")
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
            input=brief,
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
        cap = self._cap(parent)
        held = len(_occupied(plan, parent))
        if held >= cap:
            raise PlanRefusal(
                409,
                "level_full",
                f"this {parent.level} already holds {held} of the {cap} {_noun(level)} "
                "[planning] allows; remove one to add more",
                node_id=parent.id,
            )
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
        if node.id == plan.root_id and plan.generation_pending:
            if position is not None:
                raise PlanRefusal(422, "invalid_argument", "the plan's root has no siblings")
            requested = self._with_sections(node, plan.input | dict(sections), siblings=[])
            return self._write(
                plan,
                expected_revision,
                now,
                input={
                    key: plain(getattr(requested, key))
                    for key in SECTIONS
                    if key not in TASK_SECTIONS
                },
                events=[_node_changed(plan.id, node.id, "input_updated")],
                actor=actor,
            )
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
        (the issues stay; lantern stops offering the plan). ``deleted`` or
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
