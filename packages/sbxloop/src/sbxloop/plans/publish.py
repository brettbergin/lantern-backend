"""Publishing one plan level to the forge, idempotent by marker (#2341).

A level is a node's ``approved`` children — and the node itself first when
it is not on the forge yet (the root of a fresh plan). Each node, in
dependency order:

1. is looked for on the forge by its marker, among the repository's issues
   carrying its level label, open or closed — an earlier attempt that was
   interrupted may have created it;
2. is created when absent, with its rendered body and its level label on
   the create itself, so an issue of ours is never on the forge without the
   label the lookup filters on. Never the trigger or the workload label: a
   published task is inert until a person starts it;
3. is linked under its parent: a native sub-issue where the parent's forge
   has them (a child already linked is not linked twice), else a line in
   the parent's managed checklist. A cross-repository sub-issue the forge
   refuses falls back to the checklist, and the result says why;
4. is recorded ``published`` with its forge reference — one write per node,
   so a walk that dies part-way resumes where it stopped.

A node that fails is reported with the forge's words and left as it was;
the nodes that depend on it (its children, its dependents) are not
attempted. Repeating the call finishes the level and duplicates nothing.
The rules that refuse a level before anything is written live in
:class:`~sbxloop.plans.service.PlanService`.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sbxloop.config import Config
from sbxloop.errors import SbxloopError
from sbxloop.plans.hierarchy import FORGE_NAMES, repository_planning_for
from sbxloop.plans.model import ForgeRef, ForgeState, Plan, PlanNode
from sbxloop.plans.render import markers, render_body, repo_of
from sbxloop.plans.store import PlanEvent, PlanStore, StaleRevision
from sbxloop.vcs.checklist import ChecklistEntry, add_child, update_checklist
from sbxloop.vcs.github.labels import LEVEL_DESCRIPTORS, LabelSpec, ensure_label
from sbxloop.vcs.protocol import IssueOps

Outcome = Literal["created", "found", "failed"]
Linked = Literal["native", "checklist", "none"]

#: How many issues one marker lookup reads per page.
PAGE = 100
#: How long an error from the forge may run in a result.
ERROR_MAX = 500


@dataclass(frozen=True, slots=True)
class NodeResult:
    """What publishing did with one node."""

    node_id: str
    outcome: Outcome
    number: int | None = None
    url: str | None = None
    linked: Linked = "none"
    error: str | None = None
    #: Why the link is a checklist line where a native link was expected.
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "outcome": self.outcome,
            "number": self.number,
            "url": self.url,
            "linked": self.linked,
            "error": self.error,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class LevelResult:
    plan: Plan
    results: tuple[NodeResult, ...] = field(default=())

    @property
    def published(self) -> list[str]:
        return [r.node_id for r in self.results if r.outcome != "failed"]

    @property
    def failed(self) -> list[str]:
        return [r.node_id for r in self.results if r.outcome == "failed"]


class _Failed(Exception):
    """One node could not be published; the message says why."""


def level_targets(
    plan: Plan, node_id: str, *, only: Collection[str] | None = None
) -> list[PlanNode]:
    """What publishing ``node_id``'s level writes, in order: the node when
    it is not published yet, then its ``approved`` children (those ``only``
    names, when it is given — an approved re-plan's additions), each after
    the siblings it depends on and otherwise in the children's order."""
    node = plan.node(node_id)
    if node is None:
        return []
    out: list[PlanNode] = [] if node.state == "published" else [node]
    pending = [
        c
        for c in plan.children(node.id)
        if c.state == "approved" and (only is None or c.id in only)
    ]
    waiting = {c.id for c in pending}
    while pending:
        ready = next(
            (c for c in pending if not any(d in waiting for d in c.depends_on)),
            pending[0],  # a cycle the service never lets in: keep the order
        )
        out.append(ready)
        pending.remove(ready)
        waiting.discard(ready.id)
    return out


def _say(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:ERROR_MAX]


class _Walk:
    """One level's walk: the caches it keeps and the steps per node."""

    def __init__(
        self,
        ops: IssueOps,
        *,
        store: PlanStore,
        config: Config,
        plan: Plan,
        clock: Callable[[], float],
        actor: Mapping[str, Any] | None,
    ) -> None:
        self.ops = ops
        self.store = store
        self.config = config
        self.plan_id = plan.id
        self.clock = clock
        self.actor = None if actor is None else dict(actor)
        # Every node already on the forge, and each one this walk publishes.
        self.refs: dict[str, tuple[str, ForgeRef]] = {
            n.id: (n.repository, n.forge)
            for n in plan.nodes
            if n.state == "published" and n.forge is not None
        }
        self.labels: dict[tuple[str, str], str] = {}
        self.issues: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
        self.children: dict[tuple[str, int], set[tuple[str, int]]] = {}

    # -- the steps -------------------------------------------------------------

    def label(self, node: PlanNode) -> str:
        """The node's level label, made sure of once per repository."""
        key = (node.repository.casefold(), node.level)
        if key in self.labels:
            return self.labels[key]
        name = self.config.labels_for(node.repository).levels.get(node.level)
        if not name:
            raise _Failed(f"{node.repository} has no {node.level} label: planning is off there")
        spec = LabelSpec(name, *LEVEL_DESCRIPTORS[node.level], kind=node.level)
        if ensure_label(self.ops, node.repository, spec) == "failed":
            raise _Failed(f"could not make sure the label {name} exists on {node.repository}")
        self.labels[key] = name
        return name

    def marked_issues(self, repo: str, label: str) -> dict[str, dict[str, Any]]:
        """This plan's issues in ``repo`` under ``label``, open or closed,
        by node id: read once per walk, every page."""
        key = (repo.casefold(), label)
        if key not in self.issues:
            found: dict[str, dict[str, Any]] = {}
            page = 1
            while True:
                rows = self.ops.issues_list(
                    repo, state="all", labels=[label], per_page=PAGE, page=page
                )
                for row in rows:
                    if not isinstance(row, dict) or "pull_request" in row:
                        continue
                    for plan_id, node_id in markers(str(row.get("body") or "")):
                        if plan_id == self.plan_id:
                            found.setdefault(node_id, row)
                if len(rows) < PAGE:
                    break
                page += 1
            self.issues[key] = found
        return self.issues[key]

    def issue(self, node: PlanNode) -> tuple[Outcome, ForgeRef]:
        """The node's issue: the one an earlier attempt left, or a new one."""
        label = self.label(node)
        existing = self.marked_issues(node.repository, label).get(node.id)
        if existing is not None:
            state: ForgeState = "closed" if existing.get("state") == "closed" else "open"
            return "found", ForgeRef(
                number=int(existing["number"]), url=str(existing.get("html_url") or ""), state=state
            )
        body = render_body(node, dependencies=self.refs)
        created = self.ops.issue_create(node.repository, node.title, body, labels=[label])
        return "created", ForgeRef(number=created.number, url=created.url, state="open")

    def link(self, node: PlanNode, ref: ForgeRef) -> tuple[Linked, str | None]:
        """Link the node's issue under its parent's; how, and why a native
        link became a checklist line."""
        if node.parent_id is None:
            return "none", None
        if node.parent_id not in self.refs:
            raise _Failed(f"its parent {node.parent_id} is not on the forge")
        parent_repo, parent = self.refs[node.parent_id]
        planning = repository_planning_for(self.config, parent_repo)
        if planning.hierarchy == "native":
            try:
                self.sub_issue(parent_repo, parent.number, node.repository, ref.number)
                return "native", None
            except SbxloopError as exc:
                if parent_repo.casefold() == node.repository.casefold():
                    raise
                forge = FORGE_NAMES.get(str(self.config.vcs_kind_for(parent_repo)), "the forge")
                reason = (
                    f"{forge} refused the cross-repository sub-issue ({_say(exc)}); "
                    "it is listed in the parent's checklist instead"
                )
        elif planning.hierarchy == "checklist":
            reason = None
        else:
            raise _Failed(planning.reason or "the parent's forge cannot hold a plan")
        entry = ChecklistEntry(ref=f"{node.repository}#{ref.number}", title=node.title)
        update_checklist(self.ops, parent_repo, parent.number, lambda b: add_child(b, entry))
        return "checklist", reason

    def sub_issue(self, repo: str, number: int, child_repo: str, child: int) -> None:
        key = (repo.casefold(), number)
        if key not in self.children:
            self.children[key] = {
                (repo_of(row).casefold(), int(row["number"]))
                for row in self.ops.sub_issues_list(repo, number)
                if isinstance(row, dict) and isinstance(row.get("number"), int)
            }
        if (child_repo.casefold(), child) in self.children[key]:
            return
        self.ops.sub_issue_add(repo, number, child_repo=child_repo, child_number=child)
        self.children[key].add((child_repo.casefold(), child))

    def record(self, node_id: str, ref: ForgeRef) -> Plan:
        """The node published, in one write against the plan as it is now
        (someone else's edit between two nodes is not a conflict here)."""
        for _ in range(3):
            plan = self.store.get(self.plan_id)
            node = None if plan is None else plan.node(node_id)
            if plan is None or node is None:
                raise _Failed(f"{node_id} was removed from the plan while it was published")
            now = self.clock()
            try:
                return self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=[replace(node, state="published", forge=ref, updated_at=now)],
                    events=[
                        PlanEvent(
                            "plan.node.changed",
                            {
                                "plan_id": plan.id,
                                "node_id": node_id,
                                "change": "published",
                                "number": ref.number,
                            },
                        )
                    ],
                    actor=self.actor,
                )
            except StaleRevision:
                continue
        raise _Failed(f"{node_id} could not be recorded: the plan kept changing")

    # -- the walk --------------------------------------------------------------

    def publish(self, node: PlanNode, blocked: Iterable[str]) -> NodeResult:
        stopped = [d for d in (node.parent_id, *node.depends_on) if d is not None and d in blocked]
        if stopped:
            what = "its parent" if stopped[0] == node.parent_id else "it depends on"
            return NodeResult(
                node.id, "failed", error=f"not attempted: {what} {stopped[0]}, which failed"
            )
        outcome: Outcome | None = None
        ref: ForgeRef | None = None
        try:
            outcome, ref = self.issue(node)
            linked, reason = self.link(node, ref)
            self.record(node.id, ref)
        except (_Failed, SbxloopError) as exc:
            return NodeResult(
                node.id,
                "failed",
                number=None if ref is None else ref.number,
                url=None if ref is None else ref.url,
                error=_say(exc),
            )
        self.refs[node.id] = (node.repository, ref)
        return NodeResult(node.id, outcome, ref.number, ref.url, linked, None, reason)


def publish_level(
    ops: IssueOps,
    *,
    store: PlanStore,
    config: Config,
    plan: Plan,
    node_id: str,
    clock: Callable[[], float],
    actor: Mapping[str, Any] | None,
    only: Collection[str] | None = None,
) -> LevelResult:
    """Publish ``node_id``'s level of ``plan`` through ``ops``: the plan as
    it now is and what happened to each node. ``only`` narrows the level to
    the approved children it names. Recording a ``plan.published`` event is
    the caller's, with the result."""
    walk = _Walk(ops, store=store, config=config, plan=plan, clock=clock, actor=actor)
    blocked: set[str] = set()
    results: list[NodeResult] = []
    for node in level_targets(plan, node_id, only=only):
        result = walk.publish(node, blocked)
        if result.outcome == "failed":
            blocked.add(node.id)
        results.append(result)
    current = store.get(plan.id)
    return LevelResult(plan=current if current is not None else plan, results=tuple(results))
