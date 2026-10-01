"""Reconciling a published plan from the forge: the forge wins (#2342).

After publish the forge is the record. :func:`reconcile_plan` re-reads a
plan's tree and folds it into the store; it only ever *reads* the forge
(``issue_get`` and ``sub_issues_list``), so a person's edit there is never
written over. Two steps, so a plan that changed in lantern while the forge
was being read is folded again from the same reading rather than read twice:

1. :func:`read_forge` walks the tree from the root's issue down: each
   issue once, then its children — a GitHub parent's native sub-issues plus
   any line in its managed checklist (the cross-repository fallback), a
   GitLab parent's checklist — one level at a time, a task having none.
   A followed node the walk did not reach is read on its own, so a node
   removed from its parent can be told from a deleted one.
2. :func:`fold` compares the reading with the plan, node by node:

   - the issue's title, the sections under our rendered headings (read back
     by :func:`~lantern.plans.render.parse_sections`; text outside them is a
     person's and is not read) and its state (``open``/``closed``) update
     the node, and each edit is recorded as a :class:`~lantern.plans.model.Drift`
     entry until someone marks it seen;
   - a child the forge lists that the plan does not know — no marker of
     this plan, or one naming a node the plan no longer has — is adopted one
     level down with ``origin = "forge"``, its sections read from its body
     where it has our headings (its whole text as the goal where it has
     none);
   - a known node listed under another parent of the right level has moved;
   - a node its parent no longer lists, or whose issue is gone (404/410), is
     *detached*: kept with the reason, never recreated, its subtree left as
     it was; one listed again is attached again;
   - a removed marker, or a managed checklist block a person broke, is
     reported — the checklist's children are then not judged at all, so a
     broken block never detaches anything.

Sections are compared as the parser reads both sides — the forge's body
and the body lantern would render for the node — so text the parser reads
imperfectly (a level-two heading inside a goal) is never reported as an
edit nobody made. Every change is one ``plan.drift`` event in the same
transaction as the fold.

A node, or a whole walk, the forge would not answer is left as it was and
named in the result; the next reconcile tries again.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from lantern.config import Config
from lantern.errors import LanternError
from lantern.plans.hierarchy import repository_planning
from lantern.plans.model import Drift, ForgeRef, ForgeState, Plan, PlanNode, child_level
from lantern.plans.render import (
    HEADINGS,
    LIST_SECTIONS,
    free_text,
    marked,
    markers,
    parse_kind,
    parse_sections,
    render_body,
    repo_of,
)
from lantern.plans.store import (
    PlanEvent,
    PlanGone,
    PlanStore,
    Reconciled,
    StaleRevision,
    new_id,
)
from lantern.vcs.checklist import START, ChecklistMangled, parse_checklist
from lantern.vcs.protocol import IssueOps

#: An issue on the forge: its repository (casefolded) and number.
Key = tuple[str, int]

#: What the forge answers for an issue that is not there any more: GitHub
#: says 410 for a deleted issue and 404 for one it cannot find.
GONE = frozenset({404, 410})
#: The most issues one reconcile reads; a tree larger than this is read up
#: to it and the rest is named as not read.
MAX_ISSUES = 500
#: How long an error from the forge may run in a result.
ERROR_MAX = 300
#: The longest goal an adopted issue's free text becomes.
GOAL_MAX = 8000
#: Drift of these kinds is one entry per node: a later edit moves its
#: ``after`` and keeps its ``before``.
MERGED = frozenset({"title", "sections", "state"})
#: What a section is when its heading is absent from a body.
_EMPTY: dict[str, Any] = {key: () if key in LIST_SECTIONS else "" for key in HEADINGS}
_TASK_ONLY = frozenset({"kind", "verify_commands", "depends_on"})


def key_of(repo: str, number: int) -> Key:
    return (repo.casefold(), int(number))


def _node_key(node: PlanNode) -> Key | None:
    return None if node.forge is None else key_of(node.repository, node.forge.number)


def _say(exc: BaseException) -> str:
    return (" ".join(str(exc).split()) or type(exc).__name__)[:ERROR_MAX]


# -- reading ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seen:
    """One issue as the forge answered it."""

    repo: str
    number: int
    url: str
    title: str
    body: str
    state: ForgeState
    updated_at: str | None


@dataclass(slots=True)
class Read:
    """What reading one issue found: the issue (``None`` with no ``error``:
    it is gone), whether its children were looked at, and the ones found
    (``None``: they could not be told)."""

    seen: Seen | None = None
    error: str | None = None
    looked: bool = False
    children: list[Key] | None = None
    checklist_error: str | None = None


@dataclass(slots=True)
class Snapshot:
    """One reading of a plan's tree on the forge."""

    reads: dict[Key, Read] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def seen_of(row: Mapping[str, Any], repo: str, number: int) -> Seen:
    state: ForgeState = "closed" if str(row.get("state") or "") == "closed" else "open"
    updated = row.get("updated_at")
    return Seen(
        repo=repo_of(row) or repo,
        number=int(row.get("number") or number),
        url=str(row.get("html_url") or ""),
        title=str(row.get("title") or ""),
        body=str(row.get("body") or ""),
        state=state,
        updated_at=None if updated is None else str(updated),
    )


class _Reader:
    def __init__(self, ops: IssueOps, config: Config, plan: Plan) -> None:
        self.ops = ops
        self.config = config
        # The published nodes by their issue, to tell whether a parent the
        # plan knows still has children the plan follows.
        self.parents_with_children: set[Key] = set()
        for node in plan.nodes:
            parent = plan.node(node.parent_id) if node.parent_id else None
            if node.followed and parent is not None:
                key = _node_key(parent)
                if key is not None:
                    self.parents_with_children.add(key)

    def issue(self, repo: str, number: int, payload: Mapping[str, Any] | None) -> Read:
        if payload is not None and "body" in payload and "title" in payload:
            return Read(seen=seen_of(payload, repo, number))
        try:
            row = self.ops.issue_get(repo, number)
        except LanternError as exc:
            if getattr(exc, "http_status", None) in GONE:
                return Read()
            return Read(error=f"could not read {repo}#{number}: {_say(exc)}")
        return Read(seen=seen_of(row, repo, number))

    def children(
        self, key: Key, seen: Seen
    ) -> tuple[list[tuple[str, int, Mapping[str, Any] | None]] | None, str | None, str | None]:
        """The issue's children as ``(repo, number, payload)``, why its
        checklist could not be read, and why its children could not be
        listed; ``None`` children when they could not be told."""
        where = f"{seen.repo}#{seen.number}"
        try:
            entries = parse_checklist(seen.body)
        except ChecklistMangled as exc:
            return None, str(exc), None
        links: list[tuple[str, int, Mapping[str, Any] | None]] = []
        for entry in entries:
            ref_repo, _, ref_number = entry.ref.rpartition("#")
            links.append((ref_repo, int(ref_number), None))
        hierarchy = repository_planning(str(self.config.vcs_kind_for(seen.repo))).hierarchy
        if hierarchy == "native":
            try:
                rows = self.ops.sub_issues_list(seen.repo, seen.number)
            except LanternError as exc:
                return None, None, f"could not list the sub-issues of {where}: {_say(exc)}"
            for row in rows:
                if isinstance(row, dict) and isinstance(row.get("number"), int):
                    links.append((repo_of(row) or seen.repo, int(row["number"]), row))
        elif hierarchy == "checklist":
            if START not in seen.body and key in self.parents_with_children:
                return None, "the sbx-plan:children block is gone from the description", None
        else:
            return None, None, f"{where}'s children are not read: its forge cannot hold a plan"
        unique: dict[Key, tuple[str, int, Mapping[str, Any] | None]] = {}
        for link in links:
            existing = unique.get(key_of(link[0], link[1]))
            if existing is None or (existing[2] is None and link[2] is not None):
                unique[key_of(link[0], link[1])] = link
        return list(unique.values()), None, None


def read_forge(ops: IssueOps, *, plan: Plan, config: Config) -> Snapshot:
    """Read ``plan``'s tree on the forge, from its root's issue down, and
    every followed node the walk did not reach. Reads only."""
    snap = Snapshot()
    root = plan.root
    if not root.followed or root.forge is None:
        return snap
    reader = _Reader(ops, config, plan)

    def full() -> bool:
        if len(snap.reads) < MAX_ISSUES:
            return False
        note = f"stopped after reading {MAX_ISSUES} issues; the rest of the tree was not read"
        if note not in snap.errors:
            snap.errors.append(note)
        return True

    queue: deque[tuple[str, int, str, Mapping[str, Any] | None]] = deque(
        [(root.repository, root.forge.number, root.level, None)]
    )
    while queue and not full():
        repo, number, level, payload = queue.popleft()
        key = key_of(repo, number)
        if key in snap.reads:
            continue
        read = reader.issue(repo, number, payload)
        snap.reads[key] = read
        if read.error:
            snap.errors.append(read.error)
        below = child_level(level)
        if read.seen is None or below is None:
            continue
        links, checklist_error, error = reader.children(key, read.seen)
        read.looked = True
        read.checklist_error = checklist_error
        if error:
            snap.errors.append(error)
        if links is None:
            continue
        read.children = [key_of(r, n) for r, n, _ in links]
        queue.extend((r, n, below, p) for r, n, p in links)
    for node in plan.nodes:
        found = _node_key(node)
        if found is None or not node.followed or found in snap.reads or full():
            continue
        read = reader.issue(node.repository, found[1], None)
        snap.reads[found] = read
        if read.error:
            snap.errors.append(read.error)
    return snap


# -- folding ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Folded:
    """What a reading changes in a plan: the nodes to write and the
    ``plan.drift`` events that describe them."""

    upsert: tuple[PlanNode, ...] = ()
    events: tuple[PlanEvent, ...] = ()


def merge_drift(drift: Iterable[Drift], entry: Drift) -> tuple[Drift, ...]:
    """``entry`` added to ``drift``. A title, sections or state change
    merges into the unseen entry of its kind: ``before`` stays the version
    a person last saw, ``after`` becomes the forge's; an edit that returned
    to what a person saw leaves nothing."""
    entries = tuple(drift)
    if entry.change not in MERGED:
        return (*entries, entry)
    existing = next((d for d in entries if d.change == entry.change), None)
    if existing is None:
        return (*entries, entry)
    rest = tuple(d for d in entries if d is not existing)
    before = {**entry.before, **existing.before}
    after = {**existing.after, **entry.after}
    keys = [k for k in after if before.get(k) != after[k]]
    if not keys:
        return rest
    return (
        *rest,
        Drift(
            change=entry.change,
            at=entry.at,
            before={k: before.get(k) for k in keys},
            after={k: after[k] for k in keys},
            reason=entry.reason,
        ),
    )


def _plain(value: Any) -> Any:
    return list(value) if isinstance(value, tuple) else value


def _ref(node: PlanNode) -> str:
    return f"{node.repository}#{node.forge.number}" if node.forge else node.id


class _Fold:
    def __init__(self, plan: Plan, snap: Snapshot, now: float) -> None:
        self.plan = plan
        self.snap = snap
        self.now = now
        self.work: dict[str, PlanNode] = {n.id: n for n in plan.nodes}
        self.touched: dict[str, None] = {}
        self.events: list[PlanEvent] = []
        self.reached: set[str] = set()
        self.by_key: dict[Key, str] = {}
        for node in plan.nodes:
            key = _node_key(node)
            if key is not None and node.state == "published":
                self.by_key.setdefault(key, node.id)

    # -- bookkeeping ------------------------------------------------------------

    def put(self, node: PlanNode) -> None:
        self.work[node.id] = node
        self.touched[node.id] = None

    def record(self, node: PlanNode, entry: Drift, **data: Any) -> PlanNode:
        """``entry`` on ``node``'s drift and one ``plan.drift`` event."""
        self.events.append(
            PlanEvent(
                "plan.drift",
                {"plan_id": self.plan.id, "node_id": node.id, "change": entry.change, **data},
            )
        )
        return replace(node, drift=merge_drift(node.drift, entry), updated_at=self.now)

    def refs(self) -> dict[str, tuple[str, ForgeRef]]:
        return {
            n.id: (n.repository, n.forge)
            for n in self.work.values()
            if n.state == "published" and n.forge is not None
        }

    def read_of(self, node: PlanNode) -> Read | None:
        key = _node_key(node)
        return None if key is None else self.snap.reads.get(key)

    def next_position(self, parent_id: str) -> int:
        positions = [n.position for n in self.work.values() if n.parent_id == parent_id]
        return max(positions, default=-1) + 1

    # -- sections -----------------------------------------------------------------

    def resolve(self, node: PlanNode, refs: Iterable[str]) -> tuple[str, ...]:
        """``Depends on`` references as the sibling ids they name; a
        reference to anything else is not a dependency the plan holds."""
        siblings = {
            n.id for n in self.work.values() if n.parent_id == node.parent_id and n.id != node.id
        }
        out: list[str] = []
        for ref in refs:
            text = ref.strip().strip("`").strip()
            target: str | None = None
            if text in siblings:
                target = text
            elif "#" in text:
                repo, _, number = text.rpartition("#")
                if number.isdigit():
                    target = self.by_key.get(key_of(repo or node.repository, int(number)))
            if target in siblings and target not in out:
                out.append(target)
        return tuple(out)

    def section_fields(self, node: PlanNode, parsed: Mapping[str, Any]) -> dict[str, Any]:
        """The node's fields as the given sections have them (only those
        named, and only what can be read)."""
        fields: dict[str, Any] = {}
        for key, value in parsed.items():
            if key in _TASK_ONLY and node.level != "task":
                continue
            if key == "kind":
                kind = parse_kind(str(value))
                if kind is None:
                    continue
                fields["kind"], fields["workload_profile"] = kind
            elif key == "depends_on":
                fields["depends_on"] = self.resolve(node, value)
            else:
                fields[key] = value
        return fields

    @staticmethod
    def forge_sections(node: PlanNode, body: str) -> dict[str, Any]:
        """The sections ``body`` holds for ``node``. An issue a person
        wrote without our headings (one adopted from the forge) holds its
        whole text as the goal."""
        parsed = parse_sections(body)
        if not parsed and node.origin == "forge":
            return {"goal": free_text(body)[:GOAL_MAX]}
        return parsed

    def edited_sections(self, node: PlanNode, body: str) -> dict[str, Any]:
        """The sections a person changed on the forge, as the node's new
        fields: the forge's body and the body lantern would render, both
        read by the same parser, section by section."""
        theirs = self.forge_sections(node, body)
        ours = parse_sections(render_body(node, dependencies=self.refs()))
        changed = {
            key: theirs.get(key, _EMPTY[key])
            for key in HEADINGS
            if (node.level == "task" or key not in _TASK_ONLY)
            and theirs.get(key, _EMPTY[key]) != ours.get(key, _EMPTY[key])
        }
        fields = self.section_fields(node, changed)
        return {k: v for k, v in fields.items() if getattr(node, k) != v}

    # -- one node -----------------------------------------------------------------

    def content(self, node: PlanNode, read: Read) -> PlanNode:
        """Fold the issue's title, sections, state, marker and managed
        checklist into ``node``; the node as it now is."""
        seen = read.seen
        assert seen is not None and node.forge is not None  # nosec B101 - callers check
        forge = node.forge
        edited = node
        title = " ".join(seen.title.split())
        if title and title != node.title:
            edited = self.record(
                edited,
                Drift("title", self.now, {"title": node.title}, {"title": title}),
                before=node.title,
                after=title,
            )
            edited = replace(edited, title=title)
        fields = self.edited_sections(node, seen.body)
        if fields:
            edited = self.record(
                edited,
                Drift(
                    "sections",
                    self.now,
                    {k: _plain(getattr(node, k)) for k in fields},
                    {k: _plain(v) for k, v in fields.items()},
                ),
                fields=sorted(fields),
            )
            edited = replace(edited, **fields)
        if forge.state is not None and seen.state != forge.state:
            edited = self.record(
                edited,
                Drift("state", self.now, {"state": forge.state}, {"state": seen.state}),
                before=forge.state,
                after=seen.state,
            )
        missing = node.origin != "forge" and not marked(seen.body, self.plan.id, node.id)
        if missing and not forge.marker_missing:
            reason = f"the sbx-plan marker was removed from {_ref(node)}"
            edited = self.record(
                edited, Drift("marker_removed", self.now, reason=reason), reason=reason
            )
        checklist_error = read.checklist_error if read.looked else forge.checklist_error
        if checklist_error and checklist_error != forge.checklist_error:
            edited = self.record(
                edited,
                Drift("checklist_mangled", self.now, reason=checklist_error),
                reason=checklist_error,
            )
        new_forge = replace(
            forge,
            state=seen.state,
            url=seen.url or forge.url,
            marker_missing=missing,
            checklist_error=checklist_error,
        )
        if edited != node or new_forge != forge:
            edited = replace(edited, forge=replace(new_forge, updated_at=seen.updated_at))
            self.put(edited)
        return edited

    def detach(self, node: PlanNode, reason: str) -> None:
        assert node.forge is not None  # nosec B101 - only published nodes detach
        edited = self.record(node, Drift("detached", self.now, reason=reason), reason=reason)
        self.put(replace(edited, forge=replace(node.forge, detached=reason)))

    def relink(self, node: PlanNode, parent: PlanNode) -> PlanNode:
        """``node`` listed under ``parent``: attached again when it was
        detached, moved when it was under another parent."""
        assert node.forge is not None  # nosec B101 - only published nodes relink
        edited = node
        if node.parent_id != parent.id:
            if not node.forge.detached:
                edited = self.record(
                    edited,
                    Drift(
                        "moved",
                        self.now,
                        {"parent_id": node.parent_id},
                        {"parent_id": parent.id},
                    ),
                    before=node.parent_id,
                    after=parent.id,
                )
            edited = replace(edited, parent_id=parent.id, position=self.next_position(parent.id))
        if node.forge.detached:
            edited = self.record(
                edited,
                Drift("reattached", self.now, after={"parent_id": parent.id}),
                parent_id=parent.id,
            )
            edited = replace(edited, forge=replace(node.forge, detached=None))
        if edited != node:
            self.put(edited)
        return edited

    def adopt(self, parent: PlanNode, seen: Seen) -> PlanNode | None:
        """A child a person added on the forge, as a published node one
        level under ``parent``."""
        level = child_level(parent.level)
        if level is None:
            return None
        node = PlanNode(
            id=new_id("node_"),
            plan_id=self.plan.id,
            parent_id=parent.id,
            position=self.next_position(parent.id),
            level=level,
            repository=seen.repo,
            state="published",
            origin="forge",
            title=(" ".join(seen.title.split()) or f"{seen.repo}#{seen.number}")[:256],
            forge=ForgeRef(
                number=seen.number, url=seen.url, state=seen.state, updated_at=seen.updated_at
            ),
            created_at=self.now,
            updated_at=self.now,
        )
        node = replace(node, **self.section_fields(node, self.forge_sections(node, seen.body)))
        node = self.record(
            node,
            Drift(
                "adopted",
                self.now,
                after={"parent_id": parent.id, "number": seen.number, "url": seen.url},
            ),
            parent_id=parent.id,
            number=seen.number,
            url=seen.url,
        )
        self.put(node)
        return node

    # -- the walk -------------------------------------------------------------

    def listed_by(self, node: PlanNode, key: Key) -> bool:
        read = self.read_of(node)
        return read is not None and read.children is not None and key in read.children

    def walk(self) -> None:
        root = self.work[self.plan.root_id]
        key = _node_key(root)
        if not root.followed or key is None:
            return
        queue: deque[tuple[str, Key]] = deque([(root.id, key)])
        while queue:
            node_id, key = queue.popleft()
            node = self.work[node_id]
            read = self.snap.reads.get(key)
            self.reached.add(node_id)
            if read is None or read.error:
                continue
            if read.seen is None:
                self.detach(node, "its issue was deleted (or this server can no longer see it)")
                continue
            node = self.content(node, read)
            below = child_level(node.level)
            if read.children is None or below is None:
                continue
            for child_key in read.children:
                known = self.by_key.get(child_key)
                if known is not None:
                    child = self.work[known]
                    if child.level != below or known in self.reached:
                        continue
                    if child.parent_id != node.id and not _detached_or_none(child):
                        own = self.work.get(child.parent_id or "")
                        if own is not None and own.followed and self.listed_by(own, child_key):
                            continue
                    self.relink(child, node)
                    queue.append((known, child_key))
                    continue
                child_read = self.snap.reads.get(child_key)
                if child_read is None or child_read.seen is None:
                    continue
                ours = {n for p, n in markers(child_read.seen.body) if p == self.plan.id}
                if ours & set(self.work):
                    # An issue of a node the plan has that is not recorded
                    # published yet: publishing records it, not this.
                    continue
                adopted = self.adopt(node, child_read.seen)
                if adopted is not None:
                    self.by_key[child_key] = adopted.id
                    queue.append((adopted.id, child_key))

    def unreached(self) -> None:
        """Followed nodes the walk did not reach: removed from their parent,
        deleted, or under a parent whose children could not be told."""
        for original in self.plan.nodes:
            node = self.work[original.id]
            if node.id in self.reached or node.parent_id is None or not node.followed:
                continue
            parent = self.work.get(node.parent_id)
            if parent is None or not parent.followed:
                continue
            own = self.read_of(node)
            if own is None or own.error:
                continue
            if own.seen is None:
                self.detach(node, "its issue was deleted (or this server can no longer see it)")
                continue
            parent_read = self.read_of(parent)
            if (
                parent.id in self.reached
                and parent_read is not None
                and parent_read.children is not None
            ):
                self.detach(node, f"it was removed from {_ref(parent)} on the forge")
                continue
            self.content(node, own)
            self.reached.add(node.id)


def _detached_or_none(node: PlanNode) -> bool:
    return node.forge is None or node.forge.detached is not None


def as_forge_has_it(plan: Plan, node: PlanNode, *, title: str, body: str) -> PlanNode:
    """``node`` with the title and sections its issue has on the forge —
    ``title`` and ``body`` — read exactly the way a reconcile folds them in,
    so the two never disagree about whether the issue changed (#2350).
    Pure; ``node`` need not be in ``plan`` yet (an issue being attached)."""
    folding = _Fold(plan, Snapshot(), 0.0)
    edited = replace(node, **folding.edited_sections(node, body))
    title = " ".join(title.split())
    return replace(edited, title=title) if title else edited


def fold(plan: Plan, snap: Snapshot, *, now: float) -> Folded:
    """What ``snap`` changes in ``plan``: pure."""
    folding = _Fold(plan, snap, now)
    folding.walk()
    folding.unreached()
    return Folded(
        upsert=tuple(folding.work[node_id] for node_id in folding.touched),
        events=tuple(folding.events),
    )


# -- the entry point ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """A reconcile's outcome: the plan as it now is, how many ``plan.drift``
    changes it recorded, and what the forge would not answer."""

    plan: Plan
    changes: int = 0
    error: str | None = None


def reconcile_plan(
    ops: IssueOps,
    *,
    store: PlanStore,
    config: Config,
    plan: Plan,
    clock: Callable[[], float],
    actor: Mapping[str, Any] | None = None,
) -> Reconciliation:
    """Read ``plan``'s tree on the forge through ``ops`` and fold it into
    the store: the entry point the API calls on open and on sync, and the
    one an epic run's poll calls. Never writes to the forge. A plan edited
    in lantern meanwhile is folded again from the same reading."""
    snap = read_forge(ops, plan=plan, config=config)
    error = "; ".join(snap.errors)[:2000] or None
    for _ in range(3):
        current = store.get(plan.id)
        if current is None:
            raise PlanGone(plan.id)
        now = clock()
        folded = fold(current, snap, now=now)
        if not folded.upsert:
            return Reconciliation(store.mark_reconciled(plan.id, Reconciled(now, error)), 0, error)
        try:
            changed = store.apply(
                plan.id,
                expected_revision=current.revision,
                now=now,
                upsert=folded.upsert,
                events=folded.events,
                actor=None if actor is None else dict(actor),
                reconciled=Reconciled(now, error),
            )
        except StaleRevision:
            continue
        return Reconciliation(changed, len(folded.events), error)
    busy = "the plan kept changing while the forge was folded in; try again"
    return Reconciliation(store.mark_reconciled(plan.id, Reconciled(None, busy)), 0, busy)
