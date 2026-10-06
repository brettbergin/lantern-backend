"""A level's review and what keeps it current.

A review of a level is a verdict on the node's children as they were when
it was given, held as a digest of everything about them a reviewer judges.
Anything that changes what would be published — an edit, an addition, a
removal, a reorder — moves the digest, and the review is no longer current.
A review the store cannot read is no review, never a plan that will not
load.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update

from lantern.daemon.store import DaemonStore
from lantern.db.daemon_models import PlanNodeRow, PlanRow
from lantern.plans.model import (
    Plan,
    PlanNode,
    PlanReview,
    review_digest,
    review_is_current,
)
from lantern.plans.store import PlanStore

NOW = 1_800_000_000.0


def _node(node_id: str, **fields: Any) -> PlanNode:
    base: dict[str, Any] = {
        "id": node_id,
        "plan_id": "plan_1",
        "parent_id": "epic",
        "position": 0,
        "level": "task",
        "repository": "o/r",
        "state": "proposed",
        "origin": "planner",
        "title": node_id,
        "kind": "code",
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(fields)
    return PlanNode(**base)


def _plan(*children: PlanNode, root: PlanNode | None = None, **fields: Any) -> Plan:
    epic = root or _node("epic", parent_id=None, level="epic", kind=None, goal="Ship it")
    return Plan(
        id="plan_1",
        workspace_id="default",
        root_id="epic",
        archived=False,
        created_by=None,
        created_by_display=None,
        created_at=NOW,
        updated_at=NOW,
        revision=1,
        nodes=(epic, *children),
        **fields,
    )


def _level() -> Plan:
    return _plan(
        _node("a", position=0, goal="first", acceptance_criteria=("stored",)),
        _node("b", position=1, depends_on=("a",), verify_commands=("make t",)),
    )


def _changed(plan: Plan, node_id: str, **fields: Any) -> Plan:
    return replace(
        plan, nodes=tuple(replace(n, **fields) if n.id == node_id else n for n in plan.nodes)
    )


def _reviewed(plan: Plan, *, include_node: bool = False) -> Plan:
    review = PlanReview(
        run_id="r1",
        verdict="approve",
        reasons=("covers the goal",),
        digest=review_digest(plan, plan.root, include_node=include_node),
        reviewed_by="agent:critic",
        at=NOW,
    )
    return _changed(plan, "epic", review=review)


class TestTheDigest:
    def test_it_is_the_same_for_the_same_level(self) -> None:
        plan = _level()
        again = _level()
        assert review_digest(plan, plan.root) == review_digest(again, again.root)
        assert review_digest(plan, plan.root).startswith("r1-")

    def test_what_a_reviewer_does_not_judge_does_not_move_it(self) -> None:
        """Approving and publishing the level are what a review is for:
        neither they, nor a timestamp, nor who did them, make it stale."""
        plan = _level()
        before = review_digest(plan, plan.root)
        for fields in (
            {"state": "approved", "approved_by": "agent:critic"},
            {"state": "published", "published_by": "agent:critic"},
            {"updated_at": NOW + 60},
            {"proposed_by": "agent:planner"},
        ):
            moved = _changed(plan, "a", **fields)
            assert review_digest(moved, moved.root) == before, fields
        listed_differently = replace(plan, nodes=(plan.nodes[0], plan.nodes[2], plan.nodes[1]))
        assert review_digest(listed_differently, listed_differently.root) == before

    @pytest.mark.parametrize(
        "fields",
        [
            {"title": "another title"},
            {"goal": "another goal"},
            {"context": "more context"},
            {"acceptance_criteria": ("stored", "listed")},
            {"depends_on": ()},
            {"kind": "workload"},
            {"workload_profile": "research"},
            {"verify_commands": ("make test",)},
            {"non_goals": "not this"},
            {"constraints": "no new services"},
            {"repository": "o/other"},
            {"position": 5},
        ],
        ids=lambda fields: next(iter(fields)),
    )
    def test_an_edit_of_a_child_moves_it(self, fields: dict[str, Any]) -> None:
        plan = _level()
        edited = _changed(plan, "b", **fields)
        assert review_digest(edited, edited.root) != review_digest(plan, plan.root)

    def test_an_addition_a_removal_and_a_reorder_move_it(self) -> None:
        plan = _level()
        before = review_digest(plan, plan.root)
        added = replace(plan, nodes=(*plan.nodes, _node("c", position=2)))
        removed = replace(plan, nodes=plan.nodes[:2])
        swapped = _changed(_changed(plan, "a", position=1), "b", position=0)
        replaced = replace(plan, nodes=(*plan.nodes[:2], replace(plan.nodes[2], id="b2")))
        digests = {review_digest(p, p.root) for p in (added, removed, swapped, replaced)}
        assert before not in digests and len(digests) == 4

    def test_another_nodes_children_are_not_part_of_it(self) -> None:
        plan = _level()
        elsewhere = replace(plan, nodes=(*plan.nodes, _node("x", parent_id="a", title="deeper")))
        assert review_digest(elsewhere, elsewhere.root) == review_digest(plan, plan.root)

    def test_the_node_itself_is_covered_only_when_asked(self) -> None:
        """A generated root is published with its level and never approved,
        so a review of the root's level can cover the root too."""
        plan = _level()
        without = review_digest(plan, plan.root)
        with_node = review_digest(plan, plan.root, include_node=True)
        assert without != with_node
        edited = _changed(plan, "epic", goal="Ship something else")
        assert review_digest(edited, edited.root) == without
        assert review_digest(edited, edited.root, include_node=True) != with_node


class TestACurrentReview:
    def test_no_review_is_not_current(self) -> None:
        plan = _level()
        assert review_is_current(plan, plan.root) is False

    def test_a_review_of_the_level_as_it_is_now_is_current(self) -> None:
        plan = _reviewed(_level())
        assert review_is_current(plan, plan.root) is True
        approved = _changed(_changed(plan, "a", state="approved"), "b", state="approved")
        assert review_is_current(approved, approved.root) is True

    @pytest.mark.parametrize("change", ["edit", "add", "remove", "reorder"])
    def test_any_change_to_the_children_after_it_is_not(self, change: str) -> None:
        plan = _reviewed(_level())
        if change == "edit":
            after = _changed(plan, "a", goal="something else")
        elif change == "add":
            after = replace(plan, nodes=(*plan.nodes, _node("c", position=2)))
        elif change == "remove":
            after = replace(plan, nodes=plan.nodes[:2])
        else:
            after = _changed(_changed(plan, "a", position=1), "b", position=0)
        assert review_is_current(after, after.root) is False

    def test_a_review_that_covered_the_node_goes_stale_when_the_node_is_edited(self) -> None:
        plan = _reviewed(_level(), include_node=True)
        assert review_is_current(plan, plan.root) is True
        assert review_is_current(plan, plan.root, include_node=True) is True
        assert review_is_current(plan, plan.root, include_node=False) is False
        edited = _changed(plan, "epic", goal="Ship something else")
        assert review_is_current(edited, edited.root) is False

    def test_a_review_of_the_children_alone_does_not_cover_the_node(self) -> None:
        plan = _reviewed(_level())
        assert review_is_current(plan, plan.root, include_node=True) is False
        edited = _changed(plan, "epic", goal="Ship something else")
        assert review_is_current(edited, edited.root) is True


class TestTheStore:
    def _store(self, tmp_path: Path) -> PlanStore:
        return PlanStore(DaemonStore(tmp_path / "state.db"))

    def test_the_new_fields_are_kept(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        plan = _reviewed(_plan(_node("a", proposed_by="agent:planner"), goal_id="goal_1"))
        plan = replace(plan, advance="auto")
        store.create(plan, events=[], actor=None)

        read = store.get("plan_1")

        assert read is not None
        assert (read.advance, read.goal_id) == ("auto", "goal_1")
        assert read.root.review == plan.root.review
        assert review_is_current(read, read.root) is True
        child = read.node("a")
        assert child is not None and child.proposed_by == "agent:planner"
        written = store.apply(
            "plan_1",
            expected_revision=read.revision,
            now=NOW + 1,
            upsert=[replace(child, state="approved", approved_by="usr_1")],
            advance="manual",
        )
        assert written.advance == "manual" and written.goal_id == "goal_1"
        approved = written.node("a")
        assert approved is not None
        assert (approved.proposed_by, approved.approved_by, approved.published_by) == (
            "agent:planner",
            "usr_1",
            None,
        )

    def test_a_write_that_names_no_switch_leaves_it(self, tmp_path: Path) -> None:
        store = self._store(tmp_path)
        store.create(replace(_level(), advance="auto"), events=[], actor=None)
        written = store.apply("plan_1", expected_revision=1, now=NOW + 1, archived=True)
        assert written.advance == "auto"

    @pytest.mark.parametrize(
        "raw",
        [
            "not json",
            "[]",
            '"approve"',
            json.dumps({"verdict": "reject", "digest": "r1-abc", "run_id": "r1", "at": 1}),
            json.dumps({"verdict": "approve", "run_id": "r1", "at": 1}),
            json.dumps({"verdict": "approve", "digest": "r1-abc", "at": 1}),
            json.dumps(
                {"verdict": "approve", "digest": "r1-abc", "run_id": "r1", "reasons": 7, "at": 1}
            ),
            json.dumps({"verdict": "approve", "digest": "r1-abc", "run_id": "r1", "at": "then"}),
        ],
        ids=["text", "list", "string", "verdict", "no-digest", "no-run", "reasons", "at"],
    )
    def test_a_review_this_build_cannot_read_is_no_review(self, tmp_path: Path, raw: str) -> None:
        """One a later build wrote in a shape this one does not know is
        treated as none — the level is then not reviewed, which is the safe
        reading — and the rest of the plan loads."""
        store = self._store(tmp_path)
        store.create(_reviewed(_level()), events=[], actor=None)
        with store.dstore.transaction() as session:
            session.execute(
                update(PlanNodeRow).where(PlanNodeRow.node_id == "epic").values(review_json=raw)
            )

        plan = store.get("plan_1")

        assert plan is not None and [n.id for n in plan.nodes] == ["epic", "a", "b"]
        assert plan.root.review is None
        assert review_is_current(plan, plan.root) is False

    def test_an_advance_this_build_does_not_know_reads_manual(self, tmp_path: Path) -> None:
        """A value a later build stored is never read as leave to advance."""
        store = self._store(tmp_path)
        store.create(replace(_level(), advance="auto"), events=[], actor=None)
        with store.dstore.transaction() as session:
            session.execute(update(PlanRow).values(advance="supervised"))
        plan = store.get("plan_1")
        assert plan is not None and plan.advance == "manual"
