"""What every part of the plan service stands on: the store and the config
it reads, the refusal every rule raises, the busy sets that keep a publish,
a reconcile and a person's direct write from overlapping, the checks each
write runs (the level, the revision, the cap, the sections, the
dependencies), and the module-level rules the parts share. The parts are
:mod:`~lantern.plans.service_drafts`, :mod:`~lantern.plans.service_planner`,
:mod:`~lantern.plans.service_questions` and :mod:`~lantern.plans.service_forge`;
:class:`~lantern.plans.service.PlanService` is the one object callers hold.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any

from lantern.config import Config
from lantern.engine.planning import (
    Clarification,
    CurrentChild,
    PlanAnswer,
)
from lantern.errors import LanternError
from lantern.log import get_logger
from lantern.plans.direct import (
    Linked,
    issue_write,
)
from lantern.plans.forgeread import Seen, gone, say, seen_of
from lantern.plans.hierarchy import FORGE_NAMES, repository_planning_for
from lantern.plans.model import (
    CONTENT_FIELDS,
    Level,
    Plan,
    PlanNode,
    child_level,
    content,
    content_version,
)
from lantern.plans.reconcile import (
    as_forge_has_it,
    key_of,
)
from lantern.plans.render import parse_issue_url
from lantern.plans.store import (
    PlanEvent,
    PlanGone,
    PlanStore,
    StaleRevision,
    retry_stale,
)
from lantern.vcs.protocol import IssueOps

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


class _ServiceBase:
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

    def _cap(self, node: PlanNode) -> int:
        return self._cap_key(node)[0]

    def _cap_key(self, node: PlanNode) -> tuple[int, str]:
        """The cap on ``node``'s children and the ``[planning]`` key it is."""
        planning = self._config().planning_for(node.repository)
        if node.level == "initiative":
            return planning.max_epics_per_initiative, "max_epics_per_initiative"
        return planning.max_tasks_per_epic, "max_tasks_per_epic"

    def _room(self, plan: Plan, node: PlanNode) -> int:
        """How many more children the cap leaves ``node`` room for."""
        return self._cap(node) - len(_occupied(plan, node))

    def _check_sections(self, parent: PlanNode, level: Level, sections: Mapping[str, Any]) -> None:
        """An addition's sections hold to the rules a person's child does."""
        probe = PlanNode(
            id="node_probe",
            plan_id=parent.plan_id,
            parent_id=parent.id,
            position=0,
            level=level,
            repository=parent.repository,
            state="proposed",
            origin="planner",
            title="",
        )
        self._with_sections(
            probe, {k: v for k, v in sections.items() if k != "depends_on"}, siblings=[]
        )

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
        except LanternError as exc:
            raise PlanRefusal(
                503, "source_unavailable", f"could not reach the forge: {exc}"
            ) from exc

    @staticmethod
    def _read(ops: IssueOps, repo: str, number: int, *, missing: int) -> Seen:
        try:
            row = ops.issue_get(repo, number)
        except LanternError as exc:
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

        def attempt() -> Plan:
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
            except PlanGone as exc:
                raise _not_found(plan_id) from exc

        try:
            return retry_stale(attempt)
        except StaleRevision as exc:
            raise PlanRefusal(
                409,
                "already_in_progress",
                "the forge was written but the plan kept changing while it was recorded; "
                "a sync reads it back",
            ) from exc

    @staticmethod
    def _followed(plan: Plan, node_id: str) -> PlanNode:
        """``node_id``, on the forge and still following its issue."""
        node = _ServiceBase._node(plan, node_id)
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
        cap, key = self._cap_key(parent)
        held = len(_occupied(plan, parent))
        if held + 1 > cap:
            raise PlanRefusal(
                409,
                "too_many_children",
                f"{parent.title} already has {held} children; [planning] {key} is {cap}",
                cap=cap,
                children=held,
            )

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
        cap, key = self._cap_key(node)
        going = {t.id for t in targets if t.id != node.id}  # the node itself is no child
        total = len({c.id for c in _occupied(plan, node)} | going)
        if total > cap:
            raise PlanRefusal(
                409,
                "too_many_children",
                f"{node.title} would have {total} children on the forge; [planning] {key} is {cap}",
                cap=cap,
                children=total,
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
        input: dict[str, Any] | None = None,
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
                input=input,
                events=events,
                actor=dict(actor),
            )
        except StaleRevision as exc:
            raise _stale(exc) from exc
        except PlanGone as exc:
            raise _not_found(plan.id) from exc

    #: Who a plan run's writes are attributed to.

    def breakdown_target(
        self, plan_id: str, node_id: str, *, expected_revision: int | None = None
    ) -> tuple[Plan, PlanNode]:
        """The plan and the node a breakdown proposes the next level of,
        refused by name when the node cannot take one: a task has no
        children; a repository planning is off for (or no longer
        configured) cannot hold the level; a level at its cap has no room
        for a breakdown. A node on the forge with children there is
        re-planned (:func:`replanned`): its run proposes a diff, which a
        full level does not stop — it may change and close children."""
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
        held = len(_occupied(plan, node))
        if (
            not replanned(plan, node)
            and held >= cap
            and not (plan.generation_pending and node.id == plan.root_id)
        ):
            raise PlanRefusal(
                409,
                "level_full",
                f"this {node.level} already holds {held} of the {cap} "
                f"{_noun(level)} [planning] allows; remove one to propose more",
                node_id=node.id,
            )
        return plan, node

    def _write_published(
        self,
        ops: IssueOps,
        plan: Plan,
        node_id: str,
        *,
        forge_version: str,
        sections: Mapping[str, Any],
        position: int | None,
        clock: Callable[[], float],
        actor: Mapping[str, Any],
        via: str | None = None,
    ) -> Plan:
        """The one write path to a published node's issue, under the
        caller's :meth:`_forge_write`: read the issue, refuse ``409
        forge_changed`` when it no longer reads as ``forge_version``, write
        the title when it changed and in the body only the sections that
        changed, and record the node. A person's direct edit and an approved
        re-plan change both come here."""
        node = self._followed(plan, node_id)
        assert node.forge is not None  # nosec B101 - _followed checks
        where = f"{node.repository}#{node.forge.number}"
        issue_url = node.forge.url
        seen = self._read(ops, node.repository, node.forge.number, missing=409)
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
            except LanternError as exc:
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
                **({"via": via} if via else {}),
            },
            clock=clock,
            actor=actor,
        )


PLANNER: Mapping[str, str] = {
    "kind": "system",
    "id": "planner",
    "display": "the planner",
    "via": "plan run",
}


def replanned(plan: Plan, node: PlanNode) -> bool:
    """Whether a plan run for ``node`` is a re-plan (#2346): the node is on
    the forge and so is at least one of its children, followed there. Its
    answer is then a diff against them, never a fresh level."""
    return node.state == "published" and any(c.followed for c in plan.children(node.id))


def _changeable(node: PlanNode) -> bool:
    """On the forge, followed and open: a re-plan may change or close it."""
    return node.followed and node.forge is not None and node.forge.state != "closed"


def _current(child: PlanNode) -> CurrentChild:
    """A child as a re-plan's brief carries it."""
    return CurrentChild(
        id=child.id,
        title=child.title,
        state=child.state,
        origin=child.origin,
        issue="" if child.forge is None else f"{child.repository}#{child.forge.number}",
        forge_state=None if child.forge is None else child.forge.state,
        changeable=_changeable(child),
        owned=child.origin != "forge",
        goal=child.goal,
        context=child.context,
        acceptance_criteria=list(child.acceptance_criteria),
        kind=child.kind,
        workload_profile=child.workload_profile,
        verify_commands=list(child.verify_commands),
        depends_on=list(child.depends_on),
        non_goals=child.non_goals,
        constraints=child.constraints,
    )


def _kept(plan: Plan, node: PlanNode) -> list[PlanNode]:
    """The children of ``node`` a proposal leaves where they are: every one
    a person made, edited or approved, and every ``proposed`` one a person
    has since built under (a drafted or approved task under a proposed
    epic makes the epic theirs). What is left — a planner's ``proposed``
    child with nothing but the planner's own work beneath it — is what
    the next proposal replaces. One rule, so the room the brief reports,
    the room the delivery enforces and what the delivery removes agree."""
    return [child for child in plan.children(node.id) if _stays(plan, child)]


def _stays(plan: Plan, child: PlanNode) -> bool:
    if child.state != "proposed":
        return True
    return any(n.state != "proposed" for n in plan.descendants(child.id))


def _occupied(plan: Plan, node: PlanNode) -> list[PlanNode]:
    """The children of ``node`` that each take one of the level's places
    under ``[planning]``'s cap — one rule for drafting, the planner's room,
    a re-plan's room, attaching and publishing: every child that stays
    (:func:`_kept`), unless it has left its parent on the forge (detached).
    A planner's proposed child with nothing under it is replaceable and
    takes no place; a closed child still on the forge keeps its place."""
    return [
        child
        for child in _kept(plan, node)
        if not (child.forge is not None and child.forge.detached)
    ]


def _noun(level: str) -> str:
    return "epics" if level == "epic" else "tasks"


def _waiting(node: PlanNode) -> Clarification:
    waiting = node.generation
    if waiting is None:
        raise PlanRefusal(
            409, "no_questions", "no questions are waiting on this node", node_id=node.id
        )
    if waiting.status != "awaiting_answers":
        raise PlanRefusal(
            409,
            "already_answered",
            f"these questions were already {waiting.status.replace('_', ' ')}",
            node_id=node.id,
            questions_status=waiting.status,
        )
    return waiting


def _checked_answers(
    waiting: Clarification, answers: Mapping[str, PlanAnswer]
) -> dict[str, PlanAnswer]:
    """``answers`` held to the questions they answer: a question that was
    asked, a choice it offers, and free text only where it allows it."""
    by_id = {question.id: question for question in waiting.questions}
    checked: dict[str, PlanAnswer] = {}
    for qid, answer in answers.items():
        question = by_id.get(qid)
        if question is None:
            known = ", ".join(sorted(by_id))
            raise PlanRefusal(
                422, "invalid_argument", f"no question {qid!r} is waiting (asked: {known})"
            )
        value = (answer.value or "").strip() or None
        text = " ".join(answer.text.split())
        if value is not None and question.choice(value) is None:
            offered = ", ".join(c.value for c in question.choices)
            raise PlanRefusal(
                422,
                "invalid_argument",
                f"{value!r} is not a choice of question {qid!r} (choices: {offered})",
            )
        if value is None and not text:
            raise PlanRefusal(
                422, "invalid_argument", f"the answer to {qid!r} names no choice and has no text"
            )
        if value is None and not question.allow_free_text:
            raise PlanRefusal(
                422, "invalid_argument", f"question {qid!r} takes one of its choices, not text"
            )
        checked[qid] = PlanAnswer(value=value, text=text)
    return checked


def _node_changed(plan_id: str, node_id: str, change: str) -> PlanEvent:
    return PlanEvent(
        "plan.node.changed", {"plan_id": plan_id, "node_id": node_id, "change": change}
    )


def _title(plan: Plan, node_id: str) -> str:
    node = plan.node(node_id)
    return node.title if node is not None else node_id


def _deleted(plan_id: str) -> dict[str, Any]:
    return {"plan_id": plan_id, "node_id": None, "change": "deleted"}
