"""After publish the forge wins: publishing a level, reading the forge back
(open, sync, acknowledging drift), and a person's direct writes to a published
node — an edit of its issue, attaching and detaching a child (#2342, #2350).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from lantern.errors import LanternError
from lantern.log import get_logger
from lantern.plans.direct import (
    LinkRefused,
    add_level_label,
    labelled,
    link_child,
    unlink_child,
    without_dependency,
)
from lantern.plans.forgeread import gone, say, seen_of
from lantern.plans.model import (
    ForgeRef,
    Plan,
    PlanNode,
    child_level,
)
from lantern.plans.publish import LevelResult, level_targets, publish_level
from lantern.plans.reconcile import (
    Reconciliation,
    as_forge_has_it,
    reconcile_plan,
)
from lantern.plans.render import drop_reference, markers
from lantern.plans.service_base import (
    IDLE,
    NO_FORGE,
    Attached,
    PlanRefusal,
    _not_found,
    _ServiceBase,
)
from lantern.plans.store import (
    PlanEvent,
    PlanGone,
    Reconciled,
    new_id,
)
from lantern.vcs.checklist import ChecklistMangled
from lantern.vcs.protocol import IssueOps

log = get_logger(__name__)


class _ForgeWrites(_ServiceBase):
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
        :mod:`~lantern.plans.publish`). Everything that would refuse the
        level is checked before the forge is touched; ``forge_kind`` is the
        forge the daemon's connection speaks (``None``: it has none), and
        ``connect`` opens it. Records ``plan.published`` with the result."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        self._check_level(plan, node)
        if plan.generation_pending:
            raise PlanRefusal(
                409, "generation_required", "Generate the plan from its brief before publishing it"
            )
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
            except LanternError as exc:
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
        :mod:`~lantern.plans.reconcile`); never writes to the forge.
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
            except LanternError as exc:
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
        (see :mod:`~lantern.plans.direct`) and record them. ``forge_version``
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
        with self._forge_write(plan.id, expected_revision) as plan:
            ops = self._connect(forge_kind, connect)
            return self._write_published(
                ops,
                plan,
                node_id,
                forge_version=forge_version,
                sections=sections,
                position=position,
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
            except LanternError as exc:
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
            except LanternError as exc:
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
            except LanternError as exc:
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
            except LanternError as exc:
                if gone(exc):
                    continue
                raise
            body = str(row.get("body") or "")
            dropped = drop_reference(body, sibling.repository, node.repository, node.forge.number)
            if dropped != body:
                ops.issue_update(sibling.repository, sibling.forge.number, body=dropped)
                rewritten.append(sibling.id)
        return rewritten
