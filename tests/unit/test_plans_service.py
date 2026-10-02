"""The plan service's own rules, exercised directly: what a breakdown's
delivery replaces and what it leaves where it is."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lantern.config import Config
from lantern.daemon.store import DaemonStore
from lantern.engine.planning import PlanProposal, ProposedChild
from lantern.plans import PlanService
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
