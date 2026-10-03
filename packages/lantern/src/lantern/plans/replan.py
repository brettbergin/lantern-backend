"""Applying a re-plan's approved diff to the forge, through the publish path
(#2346).

A re-plan is a ``plan`` run on a node that is on the forge with children
there; its answer waits on the node as a :class:`~lantern.plans.model.Replan`
until a person approves or discards each entry. Approving writes, per
entry:

- ``modify`` — the child's issue is rewritten through the same write a
  person's direct edit takes (``PlanService._write_published``, #2350): only
  the title and the sections under our headings that change, and only while
  the issue still reads as the child did when the diff was proposed (its
  ``forge_version``) — the plan is reconciled first, so a child that
  changed since is refused naming what moved, and an issue that changed
  after that reading is refused by the write's own check. Nothing is
  written when either refuses.
- ``suggest_close`` — judged like a change first: the child must still
  read as it did when the diff was proposed (``before`` against the plan,
  ``forge_version`` against the issue read now), since a person who rewrote
  it may well want it. Then the issue is closed as not planned (GitHub's
  ``state_reason``; GitLab records no reason), and a comment on it gives
  the rationale and who approved it. The node stays in the plan, closed:
  nothing is deleted, and a person can reopen the issue. An issue already
  closed is left as it is, without a comment.
- ``add`` — the child becomes an ``approved`` node under the re-planned node
  with the id the diff minted for it, and is published by
  :func:`~lantern.plans.publish.publish_level` narrowed to the additions: its
  marker is looked for before it is created, it carries the level label and
  is linked as a sub-issue or a checklist line. An interrupted approval
  resumes by that marker and never files it twice, and an addition whose
  title a child now has (one a person filed meanwhile) is refused.

Entries that land leave the diff; one that fails stays with its error for a
person to retry or discard. Everything a person would be refused for — a
disabled repository, a full level, a dependency not on the forge — is
checked by :class:`~lantern.plans.service.PlanService` before the forge is
touched.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from lantern.config import Config
from lantern.engine.planning import fold_title
from lantern.errors import LanternError
from lantern.plans.model import Plan, PlanNode, ReplanEntry, child_level, content_version
from lantern.plans.publish import publish_level
from lantern.plans.reconcile import as_forge_has_it, seen_of
from lantern.plans.store import PlanEvent, PlanGone, PlanStore, StaleRevision
from lantern.vcs.protocol import IssueOps

Outcome = Literal["created", "found", "updated", "closed", "failed"]

#: How long an error from the forge may run in a result.
ERROR_MAX = 500
#: Sections held as tuples on a node and as lists in an entry.
_LISTS = frozenset({"acceptance_criteria", "verify_commands", "depends_on"})


@dataclass(frozen=True, slots=True)
class EntryResult:
    """What approving one entry did: an addition ``created`` (or ``found``,
    an earlier attempt's issue), a child ``updated`` or ``closed``, or
    ``failed`` with the reason in ``error`` (the entry stays in the diff).
    ``reason`` notes something that went wrong beside a success (a comment
    that could not be posted, a checklist fallback)."""

    entry_id: str
    action: str
    outcome: Outcome
    node_id: str
    number: int | None = None
    url: str | None = None
    error: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "action": self.action,
            "outcome": self.outcome,
            "node_id": self.node_id,
            "number": self.number,
            "url": self.url,
            "error": self.error,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class AppliedReplan:
    plan: Plan
    results: tuple[EntryResult, ...] = field(default=())

    def node_ids(self, *outcomes: str) -> list[str]:
        return [r.node_id for r in self.results if r.outcome in outcomes]


class EntryRefused(Exception):
    """One entry cannot be applied; the message says why."""


_Refused = EntryRefused

#: How an approved ``modify`` is written: the child as the write recorded
#: it, or :class:`EntryRefused` / :class:`~lantern.errors.LanternError`
#: with nothing written.
EditFn = Callable[[ReplanEntry], PlanNode]


def _say(exc: BaseException) -> str:
    return (" ".join(str(exc).split()) or type(exc).__name__)[:ERROR_MAX]


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def node_fields(sections: Mapping[str, Any]) -> dict[str, Any]:
    """An entry's sections as a node's fields."""
    return {k: tuple(v or ()) if k in _LISTS else v for k, v in sections.items()}


def close_comment(node: PlanNode, parent: PlanNode, rationale: str, who: str) -> str:
    """What a closed child's issue is told."""
    lines = [
        f"Closed as not planned: a re-plan of the {parent.level} “{parent.title}” found "
        f"this {node.level} is no longer needed, and {who} approved closing it.",
    ]
    if rationale.strip():
        lines += ["", *(f"> {line}" for line in rationale.strip().splitlines())]
    lines += ["", "Reopen it if it is still wanted."]
    return "\n".join(lines)


class _Apply:
    def __init__(
        self,
        ops: IssueOps,
        *,
        store: PlanStore,
        config: Config,
        plan_id: str,
        node_id: str,
        clock: Callable[[], float],
        actor: Mapping[str, Any],
        edit: EditFn,
    ) -> None:
        self.ops = ops
        self.edit = edit
        self.store = store
        self.config = config
        self.plan_id = plan_id
        self.node_id = node_id
        self.clock = clock
        self.actor = dict(actor)

    def current(self) -> Plan:
        plan = self.store.get(self.plan_id)
        if plan is None:
            raise PlanGone(self.plan_id)
        return plan

    def write(
        self, change: Callable[[Plan], tuple[Sequence[PlanNode], Sequence[PlanEvent]]]
    ) -> Plan:
        """One write against the plan as it now is: a reconcile or a
        person's edit between two entries is not a conflict here."""
        for _ in range(3):
            plan = self.current()
            upsert, events = change(plan)
            if not upsert:
                return plan
            try:
                return self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=self.clock(),
                    upsert=upsert,
                    events=events,
                    actor=self.actor,
                )
            except StaleRevision:
                continue
        raise _Refused("the plan kept changing while the re-plan was applied")

    def child(self, plan: Plan, entry: ReplanEntry) -> PlanNode:
        child = plan.node(entry.node_id)
        if child is None or child.parent_id != self.node_id:
            raise _Refused(f"{entry.node_id} is no longer a child of {self.node_id}")
        if not child.followed or child.forge is None:
            raise _Refused(f"“{child.title}” is no longer followed on the forge")
        return child

    def changed(self, node_id: str, events: list[PlanEvent], **fields: Any) -> Plan:
        def change(plan: Plan) -> tuple[list[PlanNode], list[PlanEvent]]:
            node = plan.node(node_id)
            if node is None:
                raise _Refused(f"{node_id} was removed from the plan while it was written")
            return [replace(node, updated_at=self.clock(), **fields)], events

        return self.write(change)

    # -- the entries -------------------------------------------------------------

    def modify(self, entry: ReplanEntry) -> EntryResult:
        plan = self.current()
        child = self.child(plan, entry)
        moved = [k for k, v in entry.before.items() if _plain(getattr(child, k)) != v]
        if moved:
            raise _Refused(
                f"“{child.title}” changed since the re-plan was proposed "
                f"({', '.join(sorted(moved))}); discard this entry or re-plan again"
            )
        written = self.edit(entry)
        forge = written.forge or child.forge
        assert forge is not None  # nosec B101 - self.child checked
        return EntryResult(entry.id, entry.action, "updated", child.id, forge.number, forge.url)

    def close(self, entry: ReplanEntry, parent: PlanNode) -> EntryResult:
        """An approved ``suggest_close``: judged against the issue as the
        forge has it now, like a change is. The child must still read as
        it did when the diff was proposed (``before`` on the plan,
        ``forge_version`` on the issue itself) — a person who rewrote it
        since may well want it — and an issue a person already closed is
        left as it is, without a comment."""
        plan = self.current()
        child = self.child(plan, entry)
        forge = child.forge
        assert forge is not None  # nosec B101 - self.child checked
        done = EntryResult(entry.id, entry.action, "closed", child.id, forge.number, forge.url)
        moved = [k for k, v in entry.before.items() if _plain(getattr(child, k)) != v]
        if moved:
            raise _Refused(
                f"“{child.title}” changed since the re-plan was proposed "
                f"({', '.join(sorted(moved))}); discard this entry or re-plan again"
            )
        try:
            row = self.ops.issue_get(child.repository, forge.number)
        except LanternError as exc:
            raise _Refused(
                f"could not read {child.repository}#{forge.number} before closing it: {_say(exc)}"
            ) from exc
        seen = seen_of(row, child.repository, forge.number)
        if seen.state == "closed":
            return replace(done, reason="it was already closed on the forge")
        if entry.forge_version:
            current = as_forge_has_it(plan, child, title=seen.title, body=seen.body)
            if content_version(current) != entry.forge_version:
                raise _Refused(
                    f"“{child.title}” changed on the forge since the re-plan was proposed; "
                    "nothing was closed — discard this entry or re-plan again"
                )
        self.ops.issue_close(child.repository, forge.number, reason="not_planned")
        self.changed(
            child.id,
            [
                PlanEvent(
                    "plan.node.changed",
                    {
                        "plan_id": plan.id,
                        "node_id": child.id,
                        "change": "closed",
                        "via": "replan",
                        "number": forge.number,
                    },
                )
            ],
            forge=replace(forge, state="closed"),
        )
        who = str(self.actor.get("display") or self.actor.get("id") or "a person")
        try:
            self.ops.issue_comment(
                child.repository,
                forge.number,
                close_comment(child, parent, entry.rationale, who),
            )
        except LanternError as exc:
            return replace(done, reason=f"the comment saying why could not be posted: {_say(exc)}")
        return done

    def adds(self, entries: Sequence[ReplanEntry], parent: PlanNode) -> list[EntryResult]:
        """The additions as approved children, then published as one
        narrowed level."""
        results: dict[str, EntryResult] = {}
        level = child_level(parent.level)
        assert level is not None  # nosec B101 - a re-planned node has children
        plan = self.current()
        siblings = plan.children(parent.id)
        titles = {
            fold_title(c.title): c for c in siblings if c.id not in {e.node_id for e in entries}
        }
        fresh: list[PlanNode] = []
        now = self.clock()
        for entry in entries:
            if plan.node(entry.node_id) is not None:
                continue  # an earlier attempt made it; publishing finds its issue
            title = str(entry.sections.get("title") or "")
            same = titles.get(fold_title(title))
            if same is not None:
                results[entry.id] = EntryResult(
                    entry.id,
                    entry.action,
                    "failed",
                    entry.node_id,
                    error=f"a child “{same.title}” exists already; discard this entry",
                )
                continue
            fresh.append(
                replace(
                    PlanNode(
                        id=entry.node_id,
                        plan_id=plan.id,
                        parent_id=parent.id,
                        position=len(siblings) + len(fresh),
                        level=level,
                        repository=parent.repository,
                        state="approved",
                        origin="planner",
                        title=title,
                        created_at=now,
                        updated_at=now,
                    ),
                    **node_fields({k: v for k, v in entry.sections.items() if k != "title"}),
                )
            )
        if fresh:
            self.write(
                lambda _plan: (
                    fresh,
                    [
                        PlanEvent(
                            "plan.node.changed",
                            {
                                "plan_id": self.plan_id,
                                "node_id": parent.id,
                                "change": "added",
                                "via": "replan",
                                "node_ids": [n.id for n in fresh],
                            },
                        )
                    ],
                )
            )
        going = [e for e in entries if e.id not in results]
        level_result = publish_level(
            self.ops,
            store=self.store,
            config=self.config,
            plan=self.current(),
            node_id=parent.id,
            clock=self.clock,
            actor=self.actor,
            only={e.node_id for e in going},
        )
        by_node = {r.node_id: r for r in level_result.results}
        after = level_result.plan
        for entry in going:
            published = by_node.get(entry.node_id)
            node = after.node(entry.node_id)
            if published is not None:
                results[entry.id] = EntryResult(
                    entry.id,
                    entry.action,
                    published.outcome,
                    entry.node_id,
                    published.number,
                    published.url,
                    published.error,
                    published.reason,
                )
            elif node is not None and node.state == "published" and node.forge is not None:
                results[entry.id] = EntryResult(
                    entry.id, entry.action, "found", node.id, node.forge.number, node.forge.url
                )
            else:
                results[entry.id] = EntryResult(
                    entry.id,
                    entry.action,
                    "failed",
                    entry.node_id,
                    error="it was not published; approve it again",
                )
        return [results[e.id] for e in entries]

    # -- the walk ------------------------------------------------------------------

    def run(self, entries: Sequence[ReplanEntry]) -> AppliedReplan:
        parent = self.current().node(self.node_id)
        if parent is None:
            raise PlanGone(self.plan_id)
        results: dict[str, EntryResult] = {}
        for entry in entries:
            if entry.action == "add":
                continue
            try:
                if entry.action == "modify":
                    results[entry.id] = self.modify(entry)
                else:
                    results[entry.id] = self.close(entry, parent)
            except (_Refused, LanternError) as exc:
                results[entry.id] = EntryResult(
                    entry.id, entry.action, "failed", entry.node_id, error=_say(exc)
                )
        adds = [e for e in entries if e.action == "add"]
        if adds:
            try:
                for result in self.adds(adds, parent):
                    results[result.entry_id] = result
            except (_Refused, LanternError) as exc:
                for entry in adds:
                    results.setdefault(
                        entry.id,
                        EntryResult(
                            entry.id, entry.action, "failed", entry.node_id, error=_say(exc)
                        ),
                    )
        ordered = tuple(results[e.id] for e in entries)
        plan = self.settle(ordered)
        return AppliedReplan(plan, ordered)

    def settle(self, results: Sequence[EntryResult]) -> Plan:
        """The diff without the entries that landed; a failed one keeps its
        error. An empty diff is gone."""
        by_entry = {r.entry_id: r for r in results}

        def change(plan: Plan) -> tuple[list[PlanNode], list[PlanEvent]]:
            node = plan.node(self.node_id)
            if node is None or node.replan is None:
                return [], []
            kept: list[ReplanEntry] = []
            for entry in node.replan.entries:
                result = by_entry.get(entry.id)
                if result is None:
                    kept.append(entry)
                elif result.outcome == "failed":
                    kept.append(replace(entry, error=result.error))
            replan = replace(node.replan, entries=tuple(kept)) if kept else None
            return [replace(node, replan=replan)], []

        try:
            return self.write(change)
        except _Refused:
            return self.current()


def apply_replan(
    ops: IssueOps,
    *,
    store: PlanStore,
    config: Config,
    plan: Plan,
    node_id: str,
    entry_ids: Sequence[str],
    clock: Callable[[], float],
    actor: Mapping[str, Any],
    edit: EditFn,
) -> AppliedReplan:
    """Apply the entries ``entry_ids`` names of ``node_id``'s pending
    re-plan through ``ops``: modifications (through ``edit``) and closes
    first, then the additions as one narrowed level. Recording ``plan.published`` is the
    caller's, with the result."""
    node = plan.node(node_id)
    if node is None or node.replan is None:
        return AppliedReplan(plan)
    wanted = set(entry_ids)
    entries = [e for e in node.replan.entries if e.id in wanted]
    walk = _Apply(
        ops,
        store=store,
        config=config,
        plan_id=plan.id,
        node_id=node_id,
        clock=clock,
        actor=actor,
        edit=edit,
    )
    return walk.run(entries)
