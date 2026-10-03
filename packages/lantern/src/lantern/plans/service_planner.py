"""The planner's side: what a breakdown is held to, the brief a plan run reads,
the delivery of its proposal or re-plan diff, and a person's approval or
discard of that diff (#2345, #2346).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from lantern.api.publicids import run_public_id
from lantern.engine.planning import (
    PlanBrief,
    PlanProposal,
    PlanReplan,
    ProfileRef,
    fold_title,
)
from lantern.log import get_logger
from lantern.plans.model import (
    Plan,
    PlanNode,
    Replan,
    ReplanEntry,
    child_level,
    content_version,
    plain,
)
from lantern.plans.reconcile import (
    reconcile_plan,
)
from lantern.plans.replan import AppliedReplan, EntryRefused, apply_replan
from lantern.plans.service_base import (
    PLANNER,
    PlanRefusal,
    _changeable,
    _current,
    _kept,
    _not_found,
    _noun,
    _occupied,
    _ServiceBase,
    replanned,
)
from lantern.plans.store import (
    PlanEvent,
    PlanGone,
    StaleRevision,
    new_id,
    retry_stale,
)
from lantern.vcs.protocol import IssueOps

log = get_logger(__name__)


class _Planning(_ServiceBase):
    def brief(self, plan_id: str, node_id: str, *, note: str = "") -> PlanBrief:
        """What a plan run is asked, read from the plan as it is now: a
        breakdown, or — for a node on the forge with children there — a
        re-plan carrying every current child."""
        plan, node = self.breakdown_target(plan_id, node_id)
        level = child_level(node.level)
        assert level is not None  # nosec B101 - breakdown_target refused a task
        replan = replanned(plan, node)
        children = plan.children(node.id)
        kept = children if replan else _kept(plan, node)
        parent = plan.node(node.parent_id) if node.parent_id else None
        cap = self._cap(node)
        return PlanBrief(
            input=plan.input,
            generate_root=plan.generation_pending and node.id == plan.root_id,
            mode="replan" if replan else "breakdown",
            current=[_current(child) for child in children] if replan else [],
            plan_id=plan.id,
            node_id=node.id,
            level=node.level,  # type: ignore[arg-type]
            child_level=level,  # type: ignore[arg-type]
            repository=node.repository,
            title=node.title,
            goal=node.goal,
            context=node.context,
            acceptance_criteria=list(node.acceptance_criteria),
            non_goals=node.non_goals,
            constraints=node.constraints,
            parent=(
                f"the {parent.level} “{parent.title}”"
                + (f" — {parent.goal.strip()}" if parent.goal.strip() else "")
                if parent is not None
                else ""
            ),
            kept=[] if replan else [child.title for child in kept],
            room=max(self._room(plan, node), 0),
            cap=cap,
            profiles=[
                ProfileRef(name=profile.name, description=profile.description or "")
                for profile in self._config().workloads
            ],
            repositories=sorted(
                {
                    child.repository
                    for child in kept
                    if child.repository.casefold() != node.repository.casefold()
                },
                key=str.casefold,
            ),
            note=note,
            max_questions=self._config().planning_for(node.repository).max_questions,
            clarification=node.generation,
        )

    def deliver_proposal(
        self,
        plan_id: str,
        node_id: str,
        proposal: PlanProposal,
        *,
        run_id: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
        proposed_by: str | None = None,
    ) -> tuple[Plan, int]:
        """Write a plan run's proposal under its node: the node's previous
        ``proposed`` children (and anything under them) are replaced, the
        children a person made or approved stay where they are, and each
        proposed child is added as ``proposed`` with ``origin = planner``
        and its dependencies mapped to the new ids — one write, one
        revision, held to the same rules a person's edit is. The plan may
        have moved while the planner worked, so the write is made against
        the revision it reads, and read again when another write won.

        ``proposed_by`` is the planner agent bound to the run
        (``agent:<slug>``), recorded on every node the proposal writes — its
        children, and the root it generates; ``None`` (a run that names no
        agent) records nobody rather than a guess. The event stays the
        planner's, a system actor, as it always was."""

        def attempt() -> tuple[Plan, int]:
            plan, node = self.breakdown_target(plan_id, node_id)
            if replanned(plan, node):
                raise PlanRefusal(
                    409,
                    "replan_required",
                    "children of this node were published while the planner worked; "
                    "re-plan it rather than breaking it down again",
                    node_id=node.id,
                )
            if proposal.source_input is not None and (
                proposal.source_input != plan.input or not plan.generation_pending
            ):
                raise PlanRefusal(
                    409,
                    "stale_input",
                    "the planning brief changed during generation; generate again",
                )
            upsert, remove = self._proposed_children(plan, node, proposal, now, proposed_by)
            if plan.generation_pending and node.id == plan.root_id:
                if proposal.root is None:
                    raise PlanRefusal(
                        422, "invalid_proposal", "the planner must generate the root from the brief"
                    )
                problems = proposal.root.problems()
                if problems:
                    raise PlanRefusal(422, "invalid_proposal", "; ".join(problems))
                generated = self._with_sections(node, proposal.root.model_dump(), siblings=[])
                # The content is the planner's now, not the person's whose
                # placeholder it replaces.
                upsert.append(
                    replace(
                        generated,
                        origin="planner",
                        state="proposed",
                        proposed_by=proposed_by,
                        updated_at=now,
                    )
                )
            try:
                changed = self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=upsert,
                    remove=remove,
                    events=[
                        PlanEvent(
                            "plan.generation.proposed",
                            {
                                "plan_id": plan.id,
                                "node_id": node.id,
                                "run_id": run_public_id(run_id),
                                "kind": "breakdown",
                                "count": len(proposal.children),
                            },
                            run_id=run_id,
                            item_id=item_id,
                            channel_id=channel_id,
                        )
                    ],
                    actor=dict(PLANNER),
                )
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            return changed, len(proposal.children)

        try:
            return retry_stale(attempt)
        except StaleRevision as exc:
            raise PlanRefusal(
                409, "stale_revision", "the plan kept changing while the proposal was written"
            ) from exc

    def _proposed_children(
        self,
        plan: Plan,
        node: PlanNode,
        proposal: PlanProposal,
        now: float,
        proposed_by: str | None = None,
    ) -> tuple[list[PlanNode], list[str]]:
        """The node upserts and removals that put ``proposal`` under ``node``,
        each new child recorded as proposed by ``proposed_by``."""
        level = child_level(node.level)
        assert level is not None  # nosec B101 - breakdown_target refused a task
        kept = _kept(plan, node)
        room = self._room(plan, node)
        if len(proposal.children) > room:
            raise PlanRefusal(
                409,
                "level_full",
                f"the {node.level} now has room for {max(room, 0)} more {_noun(level)}, "
                f"and {len(proposal.children)} were proposed",
                node_id=node.id,
            )
        staying = {child.id for child in kept}
        replaced = [child for child in plan.children(node.id) if child.id not in staying]
        gone = [n for child in replaced for n in (child, *plan.descendants(child.id))]
        if any(n.state == "published" for n in gone):
            raise PlanRefusal(
                409,
                "node_published",
                "a proposed child has something on the forge under it; detach it first",
                node_id=node.id,
            )
        removed = {n.id for n in gone}
        stays = [
            replace(
                child,
                position=index,
                depends_on=tuple(d for d in child.depends_on if d not in removed),
            )
            for index, child in enumerate(kept)
        ]
        try:
            order = proposal.dependencies()
        except ValueError as exc:
            raise PlanRefusal(422, "invalid_argument", str(exc)) from exc
        repository = self._child_repository(node, level, None)
        fresh: list[PlanNode] = []
        for offset, child in enumerate(proposal.children):
            sections: dict[str, Any] = {
                "title": child.title,
                "goal": child.goal,
                "context": child.context,
                "acceptance_criteria": child.acceptance_criteria,
                "non_goals": child.non_goals,
                "constraints": child.constraints,
            }
            if level == "task":
                sections |= {
                    "kind": child.kind,
                    "workload_profile": child.workload_profile,
                    "verify_commands": child.verify_commands,
                }
            base = PlanNode(
                id=new_id("node_"),
                plan_id=plan.id,
                parent_id=node.id,
                position=len(stays) + offset,
                level=level,
                repository=repository,
                state="proposed",
                origin="planner",
                title="",
                created_at=now,
                updated_at=now,
                proposed_by=proposed_by,
            )
            fresh.append(self._with_sections(base, sections, siblings=[]))
        ids = [n.id for n in fresh]
        linked = [
            replace(n, depends_on=tuple(ids[d] for d in order[index]))
            for index, n in enumerate(fresh)
        ]
        for n in linked:
            if n.depends_on:
                self._check_dependencies(n, [*stays, *(o for o in linked if o.id != n.id)])
        return [*stays, *linked], sorted(removed)

    def generation_event(
        self,
        type_: str,
        plan_id: str,
        node_id: str,
        *,
        run_id: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
        **data: Any,
    ) -> None:
        """Record ``plan.generation.started`` or ``.failed`` for a run: a
        notice about the plan that changes none of it."""
        self.store.record(
            [
                PlanEvent(
                    type_,
                    {
                        "plan_id": plan_id,
                        "node_id": node_id,
                        "run_id": run_public_id(run_id),
                        **data,
                    },
                    run_id=run_id,
                    item_id=item_id,
                    channel_id=channel_id,
                )
            ],
            now=now,
            actor=dict(PLANNER),
        )

    def deliver_replan(
        self,
        plan_id: str,
        node_id: str,
        replan: PlanReplan,
        *,
        run_id: str,
        now: float,
        item_id: str | None = None,
        channel_id: str | None = None,
        proposed_by: str | None = None,
    ) -> tuple[Plan, int]:
        """Keep a re-plan run's diff on its node, waiting for a person: it
        replaces any diff still waiting there, and writes nothing else — no
        child is added, changed or closed until an entry is approved. An
        addition that repeats a child the node has (by id or title), and an
        entry whose child left the forge while the planner worked, are left
        out and counted as ``skipped``; an empty diff clears the node's.
        Recorded as ``plan.generation.proposed`` with ``kind: "replan"``.
        ``proposed_by`` (the run's planner, ``agent:<slug>``) is kept on the
        diff: an addition approved from it is recorded as proposed by it."""

        def attempt() -> tuple[Plan, int]:
            plan, node = self.breakdown_target(plan_id, node_id)
            if not replanned(plan, node):
                raise PlanRefusal(
                    409,
                    "replan_unavailable",
                    "this node no longer has children on the forge; break it down instead",
                    node_id=node.id,
                )
            pending, counts = self._replan_entries(plan, node, replan, run_id, now)
            if pending is not None:
                pending = replace(pending, proposed_by=proposed_by)
            try:
                changed = self.store.apply(
                    plan.id,
                    expected_revision=plan.revision,
                    now=now,
                    upsert=[replace(node, replan=pending, updated_at=now)],
                    events=[
                        PlanEvent(
                            "plan.generation.proposed",
                            {
                                "plan_id": plan.id,
                                "node_id": node.id,
                                "run_id": run_public_id(run_id),
                                "kind": "replan",
                                "replan_id": None if pending is None else pending.id,
                                "count": 0 if pending is None else len(pending.entries),
                                **counts,
                            },
                            run_id=run_id,
                            item_id=item_id,
                            channel_id=channel_id,
                        )
                    ],
                    actor=dict(PLANNER),
                )
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            return changed, 0 if pending is None else len(pending.entries)

        try:
            return retry_stale(attempt)
        except StaleRevision as exc:
            raise PlanRefusal(
                409, "stale_revision", "the plan kept changing while the re-plan was written"
            ) from exc

    def _replan_entries(
        self, plan: Plan, node: PlanNode, replan: PlanReplan, run_id: str, now: float
    ) -> tuple[Replan | None, dict[str, int]]:
        """The diff as the entries a person approves, held to the rules a
        person's edit is; and how many of each kind, and how many were
        left out."""
        level = child_level(node.level)
        assert level is not None  # nosec B101 - breakdown_target refused a task
        children = plan.children(node.id)
        by_id = {c.id: c for c in children}
        titles = {fold_title(c.title) for c in children}
        try:
            order = replan.add_dependencies(list(by_id))
        except ValueError as exc:
            raise PlanRefusal(422, "invalid_argument", str(exc)) from exc
        skipped = 0
        minted: dict[int, str] = {}
        for index, child in enumerate(replan.add):
            if fold_title(child.title) in titles or (child.id and child.id in by_id):
                skipped += 1
                continue
            titles.add(fold_title(child.title))
            minted[index] = new_id("node_")
        room = self._room(plan, node)
        if len(minted) > room:
            raise PlanRefusal(
                409,
                "level_full",
                f"the {node.level} has room for {max(room, 0)} more {_noun(level)}, "
                f"and the re-plan adds {len(minted)}",
                node_id=node.id,
            )
        entries: list[ReplanEntry] = []
        for index, minted_id in minted.items():
            child = replan.add[index]
            sections: dict[str, Any] = {
                "title": child.title,
                "goal": child.goal,
                "context": child.context,
                "acceptance_criteria": list(child.acceptance_criteria),
                "non_goals": child.non_goals,
                "constraints": child.constraints,
            }
            if level == "task":
                sections |= {
                    "kind": child.kind,
                    "workload_profile": child.workload_profile,
                    "verify_commands": list(child.verify_commands),
                    "depends_on": [
                        minted[d] if isinstance(d, int) else d
                        for d in order[index]
                        if not isinstance(d, int) or d in minted
                    ],
                }
            self._check_sections(node, level, sections)
            entries.append(
                ReplanEntry(
                    id=new_id("rpe_"),
                    action="add",
                    node_id=minted_id,
                    sections=sections,
                    rationale=child.rationale,
                )
            )
        for change in replan.modify:
            target = by_id.get(change.target)
            if target is None or not _changeable(target) or target.origin == "forge":
                skipped += 1
                continue
            fields = dict(change.changes())
            if fields.get("kind") == "workload" and "verify_commands" not in fields:
                fields["verify_commands"] = []
            if fields.get("kind") == "code" and "workload_profile" not in fields:
                fields["workload_profile"] = None
            fields = {k: v for k, v in fields.items() if plain(getattr(target, k)) != plain(v)}
            if not fields:
                skipped += 1
                continue
            siblings = [c for c in children if c.id != target.id]
            self._with_sections(target, fields, siblings=siblings)
            entries.append(
                ReplanEntry(
                    id=new_id("rpe_"),
                    action="modify",
                    node_id=target.id,
                    sections={k: plain(v) for k, v in fields.items()},
                    before={k: plain(getattr(target, k)) for k in fields},
                    rationale=change.rationale,
                    forge_version=content_version(target),
                )
            )
        for close in replan.suggest_close:
            target = by_id.get(close.target)
            if target is None or not _changeable(target):
                skipped += 1
                continue
            entries.append(
                ReplanEntry(
                    id=new_id("rpe_"),
                    action="suggest_close",
                    node_id=target.id,
                    rationale=close.rationale,
                    forge_version=content_version(target),
                )
            )
        counts = {
            "add": sum(1 for e in entries if e.action == "add"),
            "modify": sum(1 for e in entries if e.action == "modify"),
            "suggest_close": sum(1 for e in entries if e.action == "suggest_close"),
            "skipped": skipped,
        }
        if not entries:
            return None, counts
        pending = Replan(
            id=new_id("replan_"),
            run_id=run_public_id(run_id),
            proposed_at=now,
            entries=tuple(entries),
        )
        return pending, counts

    def approve_replan(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        entry_ids: Sequence[str] | None,
        forge_kind: str | None,
        connect: Callable[[], IssueOps],
        clock: Callable[[], float],
        actor: Mapping[str, Any],
    ) -> AppliedReplan:
        """Apply a pending re-plan's entries — every one, or those
        ``entry_ids`` names — to the forge (see :mod:`~lantern.plans.replan`):
        additions through the publish path, changes through the guarded
        issue edit, closes as not planned. Everything that would refuse them
        is checked before the forge is touched, and the plan is read from
        the forge first so a change is judged against what it has now.
        Records ``plan.published`` with the result."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        chosen = self._replan_chosen(node, entry_ids)
        targets = [plan.node(e.node_id) for e in chosen if e.action != "add"]
        repos = list(
            dict.fromkeys([node.repository, *(t.repository for t in targets if t is not None)])
        )
        for repo in repos:
            self._check_publishable(repo, forge_kind)
        adds = [e for e in chosen if e.action == "add"]
        self._check_replan_adds(plan, node, adds)
        with self._forge_write(plan.id, expected_revision) as plan:
            ops = self._connect(forge_kind, connect)
            try:
                reconcile_plan(
                    ops,
                    store=self.store,
                    config=self._config(),
                    plan=plan,
                    clock=clock,
                    actor=actor,
                )
            except PlanGone as exc:
                raise _not_found(plan_id) from exc
            except Exception as exc:  # a change or a close still reads its issue first
                log.warning("plans.replan_reconcile_failed", plan_id=plan_id, error=repr(exc))

            def edit(entry: ReplanEntry) -> PlanNode:
                """An approved ``modify``, through the direct edit's write:
                refused unless the issue still reads as the child did when
                the diff was proposed."""
                latest = self.get(plan_id)
                try:
                    written = self._write_published(
                        ops,
                        latest,
                        entry.node_id,
                        forge_version=entry.forge_version or "",
                        sections=entry.sections,
                        position=None,
                        clock=clock,
                        actor=actor,
                        via="replan",
                    )
                except PlanRefusal as exc:
                    if exc.code == "forge_changed":
                        child = latest.node(entry.node_id)
                        raise EntryRefused(
                            f"“{child.title if child else entry.node_id}” changed on the forge "
                            "since the re-plan was proposed; nothing was written — discard "
                            "this entry or re-plan again"
                        ) from exc
                    raise EntryRefused(exc.detail) from exc
                return self._node(written, entry.node_id)

            result = apply_replan(
                ops,
                store=self.store,
                config=self._config(),
                plan=self.get(plan_id),
                node_id=node.id,
                entry_ids=[e.id for e in chosen],
                clock=clock,
                actor=actor,
                edit=edit,
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
                            "replan": True,
                            "published": result.node_ids("created", "found"),
                            "modified": result.node_ids("updated"),
                            "closed": result.node_ids("closed"),
                            "failed": result.node_ids("failed"),
                        },
                    )
                ],
                actor=dict(actor),
            )
            return AppliedReplan(self.get(plan_id), result.results)

    def discard_replan(
        self,
        plan_id: str,
        node_id: str,
        *,
        expected_revision: int,
        entry_ids: Sequence[str] | None,
        now: float,
        actor: Mapping[str, Any],
    ) -> Plan:
        """Drop a pending re-plan's entries — every one, or those
        ``entry_ids`` names — without writing anything to the forge."""
        plan = self.get(plan_id)
        self._check_revision(plan, expected_revision)
        self._not_archived(plan)
        node = self._node(plan, node_id)
        chosen = {e.id for e in self._replan_chosen(node, entry_ids)}
        assert node.replan is not None  # nosec B101 - _replan_chosen checked
        kept = tuple(e for e in node.replan.entries if e.id not in chosen)
        replan = replace(node.replan, entries=kept) if kept else None
        return self._write(
            plan,
            expected_revision,
            now,
            upsert=[replace(node, replan=replan, updated_at=now)],
            events=[
                PlanEvent(
                    "plan.node.changed",
                    {
                        "plan_id": plan.id,
                        "node_id": node.id,
                        "change": "replan_discarded",
                        "entry_ids": sorted(chosen),
                    },
                )
            ],
            actor=actor,
        )

    @staticmethod
    def _replan_chosen(node: PlanNode, entry_ids: Sequence[str] | None) -> list[ReplanEntry]:
        if node.replan is None or not node.replan.entries:
            raise PlanRefusal(
                409,
                "no_replan",
                f"no re-plan of {node.title} is waiting; ask for one with a breakdown",
                node_id=node.id,
            )
        entries = list(node.replan.entries)
        if entry_ids is None:
            return entries
        unknown = sorted(set(entry_ids) - {e.id for e in entries})
        if unknown:
            raise PlanRefusal(
                422,
                "invalid_argument",
                f"not entries of the waiting re-plan: {', '.join(unknown)}",
                entry_ids=unknown,
            )
        wanted = set(entry_ids)
        return [e for e in entries if e.id in wanted]

    def _check_replan_adds(self, plan: Plan, node: PlanNode, adds: Sequence[ReplanEntry]) -> None:
        """The additions fit the level's cap, and each depends only on
        children on the forge or additions approved with it."""
        if not adds:
            return
        cap, key = self._cap_key(node)
        total = len({c.id for c in _occupied(plan, node)} | {e.node_id for e in adds})
        if total > cap:
            raise PlanRefusal(
                409,
                "too_many_children",
                f"{node.title} would have {total} children; [planning] {key} is {cap}",
                cap=cap,
                children=total,
            )
        going = {e.node_id for e in adds}
        missing: dict[str, list[str]] = {}
        for entry in adds:
            for dep in entry.sections.get("depends_on") or ():
                sibling = plan.node(dep)
                if dep in going or (sibling is not None and sibling.state == "published"):
                    continue
                missing.setdefault(entry.id, []).append(dep)
        if missing:
            named = "; ".join(
                f"{entry} depends on {', '.join(deps)}" for entry, deps in missing.items()
            )
            raise PlanRefusal(
                409,
                "dependency_unpublished",
                f"approve what these additions depend on with them: {named}",
                entry_ids=sorted(missing),
            )
