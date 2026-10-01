"""The shapes of a plan and its nodes.

A node's sections are a superset of the in-run ``TaskSpec`` (title, goal,
acceptance criteria, verify commands, dependencies), so a published task
is exactly the brief a run wants. A node moves ``draft → proposed →
approved → published``: ``proposed`` means the planner wrote it and no
person has touched it, editing a proposed or approved node makes it
``draft`` again, and a published node follows its issue on the forge.

After publish the forge wins (#2342): reconciliation folds a person's
forge edits into the node and records each as a :class:`Drift` entry until
someone marks it seen, and a node whose issue left its parent (or the
forge) is *detached* — kept, said so, never recreated.

A published node may carry a pending :class:`Replan` (#2346): the planner's
diff against its children — children to add, changes to a child's
sections, children to close — waiting for a person to approve or discard
each entry. Nothing in it is on the forge until it is approved.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from lantern.engine.planning import Clarification

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


#: What reconciliation saw change on the forge (#2342): a person's edit of
#: the title, the rendered sections or the state; a child adopted, moved,
#: detached or attached again; a marker removed or a managed checklist
#: broken.
DriftChange = Literal[
    "title",
    "sections",
    "state",
    "adopted",
    "moved",
    "detached",
    "reattached",
    "marker_removed",
    "checklist_mangled",
]
DRIFT_CHANGES: tuple[DriftChange, ...] = get_args(DriftChange)


@dataclass(frozen=True, slots=True)
class ForgeRef:
    number: int
    url: str
    state: ForgeState | None
    #: The issue's ``updated_at`` as last read (the version a direct edit
    #: names is :func:`content_version`, not this: see there).
    updated_at: str | None = None
    #: Why the node no longer follows its issue (it left its parent, or the
    #: forge); ``None`` while it does.
    detached: str | None = None
    #: The issue's body lost this node's ``sbx-plan`` marker.
    marker_missing: bool = False
    #: Why the parent's managed children checklist could not be read.
    checklist_error: str | None = None


@dataclass(frozen=True, slots=True)
class Drift:
    """One change the forge made to a node that nobody has looked at yet.

    ``before`` is the node as it was last seen by a person and ``after`` as
    the forge now has it, so a client shows a diff; a second edit of the
    same kind before anyone looks moves ``after`` and keeps ``before``."""

    change: DriftChange
    at: float
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "change": self.change,
            "at": self.at,
            "before": dict(self.before),
            "after": dict(self.after),
            "reason": self.reason,
        }


ReplanAction = Literal["add", "modify", "suggest_close"]


@dataclass(frozen=True, slots=True)
class ReplanEntry:
    """One entry of a re-plan's diff.

    ``add``: ``node_id`` is the id the new child will have (minted with the
    diff, so its issue's marker is the same on every attempt) and
    ``sections`` are the whole child's, ``depends_on`` naming node ids.
    ``modify``: ``node_id`` is the child changed, ``sections`` only what
    changes, and ``before`` those sections as the child had them when the
    diff was proposed (``forge_version`` is that version of its issue).
    ``suggest_close``: ``node_id`` is the child to close. ``error`` is why
    the last attempt to apply it failed."""

    id: str
    action: ReplanAction
    node_id: str
    sections: dict[str, Any] = field(default_factory=dict)
    before: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    error: str | None = None
    #: A ``modify`` or ``suggest_close``: the child's issue version
    #: (:func:`content_version`) when the diff was proposed — a change is
    #: written only while the issue still reads so.
    forge_version: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "node_id": self.node_id,
            "sections": dict(self.sections),
            "before": dict(self.before),
            "rationale": self.rationale,
            "error": self.error,
            "forge_version": self.forge_version,
        }


@dataclass(frozen=True, slots=True)
class Replan:
    """A re-plan's diff waiting on a node: the run that proposed it, when,
    and the entries not yet applied or discarded."""

    id: str
    run_id: str | None
    proposed_at: float
    entries: tuple[ReplanEntry, ...] = ()

    def entry(self, entry_id: str) -> ReplanEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "proposed_at": self.proposed_at,
            "entries": [e.as_dict() for e in self.entries],
        }


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
    #: What the forge changed that nobody has marked seen, oldest first.
    drift: tuple[Drift, ...] = ()
    #: The latest clarifying questions a breakdown of this node asked, and
    #: what a person answered (#2345); None until one asks.
    generation: Clarification | None = None
    #: A re-plan's diff waiting for a person (#2346), or ``None``.
    replan: Replan | None = None

    @property
    def followed(self) -> bool:
        """On the forge and still following its issue."""
        return self.state == "published" and self.forge is not None and not self.forge.detached


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
    #: When the forge was last read into the plan, and what stopped the
    #: last attempt (or part of it), if anything.
    reconciled_at: float | None = None
    reconcile_error: str | None = None
    #: The person's planning brief, separate from every issue's content.
    input: dict[str, Any] = field(default_factory=dict)

    @property
    def generation_pending(self) -> bool:
        return bool(self.input) and self.root.origin == "person" and self.root.state != "published"

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


#: The fields a node's issue holds and a direct edit writes: its title and
#: the sections under lantern's rendered headings.
CONTENT_FIELDS: tuple[str, ...] = (
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


def content(node: PlanNode) -> dict[str, Any]:
    """The node's title and sections, JSON-shaped."""
    return {
        key: list(value) if isinstance(value, tuple) else value
        for key in CONTENT_FIELDS
        for value in (getattr(node, key),)
    }


def content_version(node: PlanNode) -> str:
    """The version of a published node's issue a direct edit names (#2350):
    a digest of the title and sections as the node holds them — which is
    the issue as lantern last read or wrote it. The forge's ``updated_at``
    is not used: a comment, a label or lantern's own checklist and
    sub-issue writes move it, and it covers text a direct edit never
    writes, so it would refuse edits nothing stood in the way of. The
    digest covers exactly what an edit overwrites."""
    raw = json.dumps(content(node), sort_keys=True, separators=(",", ":"))
    return "c1-" + hashlib.sha256(raw.encode()).hexdigest()[:32]
