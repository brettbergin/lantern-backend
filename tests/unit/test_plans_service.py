"""The plan service's own rules, exercised directly: what a breakdown's
delivery replaces and what it leaves where it is."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lantern.config import Config
from lantern.daemon.store import DaemonStore
from lantern.engine.planning import PlanProposal, ProposedChild
from lantern.plans import PlanRefusal, PlanService
from lantern.plans.store import PlanStore

PERSON = {"kind": "person", "id": "p1", "display": "Pat"}
REPO = "o/r"


@pytest.fixture
def plans(tmp_path: Path) -> PlanService:
    config = Config.model_validate({"home": str(tmp_path / "state"), "github": {"repo": REPO}})
    return PlanService(PlanStore(DaemonStore(config.paths.state_db)), lambda: config)


def _proposed(plans: PlanService, plan_id: str, node_id: str) -> None:
    """Make ``node_id`` look like the planner proposed it last time."""
    plan = plans.get(plan_id)
    node = plan.node(node_id)
    assert node is not None
    plans.store.apply(
        plan.id,
        expected_revision=plan.revision,
        now=3.0,
        upsert=[replace(node, state="proposed", origin="planner")],
    )


def _add(plans: PlanService, plan_id: str, parent_id: str, title: str, **sections: object) -> str:
    plan = plans.get(plan_id)
    _, node_id = plans.add_node(
        plan.id,
        expected_revision=plan.revision,
        parent_id=parent_id,
        repository=None,
        sections={"title": title, **sections},
        position=None,
        now=2.0,
        actor=PERSON,
    )
    return node_id


class TestWhatABreakdownReplaces:
    def setup_plan(self, plans: PlanService) -> tuple[str, str]:
        plan = plans.create(
            level="initiative",
            repository=REPO,
            sections={"title": "Reports", "goal": "people can export their reports"},
            now=1.0,
            actor=PERSON,
        )
        # The root was generated from the brief in an earlier run; these
        # tests are about the level under it.
        plans.store.apply(
            plan.id,
            expected_revision=plan.revision,
            now=1.5,
            upsert=[replace(plan.root, origin="planner")],
        )
        return plan.id, plan.root_id

    def test_a_proposed_child_with_nothing_under_it_is_replaced(self, plans: PlanService) -> None:
        plan_id, root = self.setup_plan(plans)
        stale = _add(plans, plan_id, root, "Old proposal")
        _proposed(plans, plan_id, stale)

        changed, count = plans.deliver_proposal(
            plan_id,
            root,
            PlanProposal(children=[ProposedChild(title="Reports API")]),
            run_id="run_1",
            now=5.0,
        )

        assert count == 1
        assert changed.node(stale) is None
        assert [c.title for c in changed.children(root)] == ["Reports API"]

    def test_a_proposed_child_a_person_built_under_stays(self, plans: PlanService) -> None:
        """A proposed epic a person drafted tasks under is theirs now: a
        new breakdown of the initiative leaves it, and everything under
        it, where it is — and the planner is told it stays."""
        plan_id, root = self.setup_plan(plans)
        epic = _add(plans, plan_id, root, "Export epic")
        _proposed(plans, plan_id, epic)
        task = _add(plans, plan_id, epic, "Person's task", kind="code")
        other = _add(plans, plan_id, root, "Empty proposal")
        _proposed(plans, plan_id, other)

        brief = plans.brief(plan_id, root)
        assert brief.kept == ["Export epic"]

        changed, _ = plans.deliver_proposal(
            plan_id,
            root,
            PlanProposal(children=[ProposedChild(title="Reports API")]),
            run_id="run_1",
            now=5.0,
        )

        assert changed.node(other) is None, "an empty proposed child is still replaced"
        kept = changed.node(epic)
        assert kept is not None and kept.state == "proposed"
        assert changed.node(task) is not None, "the person's task was deleted"
        assert [c.title for c in changed.children(root)] == ["Export epic", "Reports API"]

    def test_a_proposed_child_with_only_proposed_children_is_replaced(
        self, plans: PlanService
    ) -> None:
        plan_id, root = self.setup_plan(plans)
        epic = _add(plans, plan_id, root, "Export epic")
        _proposed(plans, plan_id, epic)
        task = _add(plans, plan_id, epic, "Planner's task", kind="code")
        _proposed(plans, plan_id, task)

        changed, _ = plans.deliver_proposal(
            plan_id,
            root,
            PlanProposal(children=[ProposedChild(title="Reports API")]),
            run_id="run_1",
            now=5.0,
        )

        assert changed.node(epic) is None and changed.node(task) is None

    def test_what_stays_counts_against_the_room(self, plans: PlanService) -> None:
        """The kept proposed epic takes one of the cap's places, so the
        room the brief reports and the room delivery enforces agree."""
        plan_id, root = self.setup_plan(plans)
        epic = _add(plans, plan_id, root, "Export epic")
        _proposed(plans, plan_id, epic)
        _add(plans, plan_id, epic, "Person's task", kind="code")

        cap = plans.brief(plan_id, root).room + 1
        assert cap == plans._cap(plans.get(plan_id).root)


def _plans_with(tmp_path: Path, **planning: object) -> PlanService:
    config = Config.model_validate(
        {"home": str(tmp_path / "state"), "github": {"repo": REPO}, "planning": planning}
    )
    return PlanService(PlanStore(DaemonStore(config.paths.state_db)), lambda: config)


def _on_forge(plans: PlanService, plan_id: str, node_id: str, number: int, **forge: object) -> None:
    from lantern.plans.model import ForgeRef

    plan = plans.get(plan_id)
    node = plan.node(node_id)
    assert node is not None
    ref = ForgeRef(number=number, url=f"https://github.com/o/r/issues/{number}", state="open")
    plans.store.apply(
        plan.id,
        expected_revision=plan.revision,
        now=4.0,
        upsert=[replace(node, state="published", forge=replace(ref, **forge))],  # type: ignore[arg-type]
    )


class TestOneCapRule:
    """What occupies a place under ``[planning]``'s cap is one rule for
    every path — drafting, the planner's room, a re-plan's room, attaching
    and publishing: every child that stays, unless it has left its parent
    on the forge. A planner's proposed child with nothing under it is
    replaceable and takes no place; a detached one has gone."""

    def _epic(self, plans: PlanService) -> tuple[str, str]:
        plan = plans.create(
            level="epic",
            repository=REPO,
            sections={"title": "Export", "goal": "exports work"},
            now=1.0,
            actor=PERSON,
        )
        plans.store.apply(
            plan.id,
            expected_revision=plan.revision,
            now=1.5,
            upsert=[replace(plan.root, origin="planner")],
        )
        return plan.id, plan.root_id

    def test_drafting_is_refused_at_the_cap_not_at_publish(self, tmp_path: Path) -> None:
        plans = _plans_with(tmp_path, max_tasks_per_epic=2)
        plan_id, epic = self._epic(plans)
        _add(plans, plan_id, epic, "One", kind="code")
        _add(plans, plan_id, epic, "Two", kind="code")
        with pytest.raises(PlanRefusal) as caught:
            _add(plans, plan_id, epic, "Three", kind="code")
        assert (caught.value.status, caught.value.code) == (409, "level_full")
        assert "2 of the 2 tasks" in caught.value.detail

    def test_a_child_that_left_its_parent_on_the_forge_frees_its_place(
        self, tmp_path: Path
    ) -> None:
        plans = _plans_with(tmp_path, max_tasks_per_epic=2)
        plan_id, epic = self._epic(plans)
        gone = _add(plans, plan_id, epic, "Gone", kind="code")
        _on_forge(plans, plan_id, gone, 7, detached="removed from the epic on the forge")
        _add(plans, plan_id, epic, "Stays", kind="code")
        # One place is taken, so one is left — for a draft and for the planner alike.
        assert plans.brief(plan_id, epic).room == 1
        _add(plans, plan_id, epic, "Fills", kind="code")
        with pytest.raises(PlanRefusal) as caught:
            _add(plans, plan_id, epic, "One too many", kind="code")
        assert caught.value.code == "level_full"
        with pytest.raises(PlanRefusal) as caught:
            plans.brief(plan_id, epic)
        assert caught.value.code == "level_full"

    def test_a_replaceable_proposed_child_takes_no_place_in_a_replan(self, tmp_path: Path) -> None:
        """A re-plan's room was counted over every child, a proposed one
        the next proposal would replace included; it is the same count
        as everywhere else now."""
        from lantern.engine.planning import PlanReplan, ReplanAddition

        plans = _plans_with(tmp_path, max_tasks_per_epic=3)
        plan_id, epic = self._epic(plans)
        _on_forge(plans, plan_id, epic, 1)
        kept = _add(plans, plan_id, epic, "Kept", kind="code")
        _on_forge(plans, plan_id, kept, 2)
        stale = _add(plans, plan_id, epic, "Stale proposal", kind="code")
        _proposed(plans, plan_id, stale)

        replan = PlanReplan(
            add=[
                ReplanAddition(
                    title="New one",
                    goal="g",
                    context="c",
                    acceptance_criteria=["a"],
                    kind="code",
                    verify_commands=["make test"],
                    rationale="r",
                ),
                ReplanAddition(
                    title="New two",
                    goal="g",
                    context="c",
                    acceptance_criteria=["a"],
                    kind="code",
                    verify_commands=["make test"],
                    rationale="r",
                ),
            ]
        )
        _, added = plans.deliver_replan(plan_id, epic, replan, run_id="run_2", now=9.0)
        assert added == 2
