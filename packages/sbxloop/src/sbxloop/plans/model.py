"""The shapes of a plan and its nodes.

A node's sections are a superset of the in-run ``TaskSpec`` (title, goal,
acceptance criteria, verify commands, dependencies), so a published task
is exactly the brief a run wants. A node moves ``draft → proposed →
approved → published``: ``proposed`` means the planner wrote it and no
person has touched it, editing a proposed or approved node makes it
``draft`` again, and a published node follows its issue on the forge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

Level = Literal["initiative", "epic", "task"]
LEVELS: tuple[Level, ...] = get_args(Level)

NodeState = Literal["draft", "proposed", "approved", "published"]
NODE_STATES: tuple[NodeState, ...] = get_args(NodeState)

Origin = Literal["person", "planner", "forge"]
TaskKind = Literal["code", "workload"]
ForgeState = Literal["open", "closed"]

#: A plan is a draft until something of it is on the forge, then published;
#: archiving keeps a published plan's record without offering it.
PlanState = Literal["draft", "published", "archived"]


def child_level(level: str) -> Level | None:
    """The level a node of ``level`` breaks down into; a task is a leaf."""
    return {"initiative": "epic", "epic": "task"}.get(level)  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class ForgeRef:
    number: int
    url: str
    state: ForgeState | None


@dataclass(frozen=True, slots=True)
class PlanNode:
    id: str
    plan_id: str
    parent_id: str | None
    position: int
    level: Level
    repository: str
    state: NodeState
    origin: Origin
    title: str
    goal: str = ""
    context: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    kind: TaskKind | None = None
    workload_profile: str | None = None
    verify_commands: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    non_goals: str = ""
    constraints: str = ""
    forge: ForgeRef | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True, slots=True)
class Plan:
    id: str
    workspace_id: str
    root_id: str
    archived: bool
    created_by: str | None
    created_by_display: str | None
    created_at: float
    updated_at: float
    revision: int
    #: Every node, the root first, then each parent's children in order.
    nodes: tuple[PlanNode, ...] = field(default=())

    @property
    def root(self) -> PlanNode:
        return next(node for node in self.nodes if node.id == self.root_id)

    @property
    def state(self) -> PlanState:
        if self.archived:
            return "archived"
        if any(node.state == "published" for node in self.nodes):
            return "published"
        return "draft"

    def node(self, node_id: str) -> PlanNode | None:
        return next((node for node in self.nodes if node.id == node_id), None)

    def children(self, node_id: str) -> list[PlanNode]:
        return sorted(
            (node for node in self.nodes if node.parent_id == node_id),
            key=lambda node: (node.position, node.id),
        )

    def descendants(self, node_id: str) -> list[PlanNode]:
        out: list[PlanNode] = []
        for child in self.children(node_id):
            out.append(child)
            out.extend(self.descendants(child.id))
        return out
