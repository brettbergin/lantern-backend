"""Who proposed, approved and published a node, kept on the node.

An event says who did something once; the node says who its content is
from and who let it through, for as long as that stays true. The planner's
children carry the agent bound to the run that proposed them (``None`` when
the run names none — never a guess), a person's node carries the person, an
approval and a publish carry whoever made them, and an edit that makes a
child a draft again takes its approval with it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from lantern.agents.assignment import AgentAssignment, AgentBinding
from lantern.config import Config
from lantern.daemon.model import WorkItem
from lantern.daemon.store import DaemonStore
from lantern.db.api_models import ApiEventRow
from lantern.engine.planning import PlanProposal, PlanReplan, ProposedChild, ProposedRoot
from lantern.plans import Plan, PlanService
from lantern.plans.generation import PlanGeneration
from lantern.plans.store import PlanStore
from tests.fakes.fake_github import FakeGithub

REPO = "o/r"
PAT = {"kind": "client", "id": "usr_pat", "display": "Pat", "via": "api"}
OWNER = {"kind": "client", "id": "usr_owner", "display": "Olu", "via": "api"}
ROOT = ProposedRoot(
    title="Export reports",
    goal="People download their reports",
    context="The reports module renders them today",
    acceptance_criteria=["A report downloads as CSV"],
)


@pytest.fixture
def plans(tmp_path: Path) -> PlanService:
    config = Config.model_validate({"home": str(tmp_path / "state"), "github": {"repo": REPO}})
    return PlanService(PlanStore(DaemonStore(config.paths.state_db)), lambda: config)


def _draft(plans: PlanService) -> Plan:
    return plans.create(
        level="epic",
        repository=REPO,
        sections={"title": "Export reports", "goal": "download them"},
        now=1.0,
        actor=PAT,
    )


def _proposal(*titles: str) -> PlanProposal:
    return PlanProposal(
        root=ROOT, children=[ProposedChild(title=title, kind="code") for title in titles]
    )


def _generated(plans: PlanService, *, proposed_by: str | None = "agent:planner") -> Plan:
    """A plan whose root and two tasks a plan run proposed."""
    plan = _draft(plans)
    changed, _ = plans.deliver_proposal(
        plan.id, plan.root_id, _proposal("A", "B"), run_id="run_1", now=2.0, proposed_by=proposed_by
    )
    return changed


def _child(plan: Plan, title: str) -> Any:
    return next(n for n in plan.nodes if n.title == title)


def _actors(plan: Plan, title: str) -> tuple[str | None, str | None, str | None]:
    node = _child(plan, title)
    return node.proposed_by, node.approved_by, node.published_by


def _approve(plans: PlanService, plan: Plan, actor: dict[str, str], *titles: str) -> Plan:
    return plans.approve(
        plan.id,
        plan.root_id,
        expected_revision=plan.revision,
        node_ids=[_child(plan, t).id for t in titles] or None,
        now=5.0,
        actor=actor,
    )


def _publish(plans: PlanService, plan: Plan, fake: FakeGithub, actor: dict[str, str]) -> Plan:
    result = plans.publish(
        plan.id,
        plan.root_id,
        expected_revision=plan.revision,
        forge_kind="github",
        connect=lambda: fake,
        clock=lambda: 9.0,
        actor=actor,
    )
    assert result.failed == [], [r.error for r in result.results]
    return result.plan


def _event_actors(plans: PlanService, type_: str) -> list[dict[str, Any]]:
    with plans.store.dstore.read() as session:
        rows = session.scalars(
            select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
        )
        return [json.loads(row.actor_json or "{}") for row in rows]


class TestWhoProposed:
    def test_a_persons_plan_and_nodes_are_theirs(self, plans: PlanService) -> None:
        plan = _draft(plans)
        assert plan.root.proposed_by == "usr_pat"
        generated = _generated(plans)
        changed, node_id = plans.add_node(
            generated.id,
            expected_revision=generated.revision,
            parent_id=generated.root_id,
            repository=None,
            sections={"title": "Mine", "kind": "code"},
            position=None,
            now=3.0,
            actor=OWNER,
        )
        added = changed.node(node_id)
        assert added is not None
        assert (added.proposed_by, added.approved_by, added.published_by) == (
            "usr_owner",
            None,
            None,
        )

    def test_the_planners_root_and_children_are_the_bound_agents(self, plans: PlanService) -> None:
        plan = _generated(plans, proposed_by="agent:architect")
        assert plan.root.origin == "planner" and plan.root.proposed_by == "agent:architect"
        assert _actors(plan, "A") == ("agent:architect", None, None)
        assert _actors(plan, "B") == ("agent:architect", None, None)
        # The event is attributed as it always was: the planner, a system actor.
        (actor,) = _event_actors(plans, "plan.generation.proposed")
        assert (actor["kind"], actor["id"]) == ("system", "planner")

    def test_a_run_that_names_no_agent_records_nobody(self, plans: PlanService) -> None:
        """Never a guess: the root stops being the person's (its content is
        the planner's now) and is nobody's by name."""
        plan = _generated(plans, proposed_by=None)
        assert plan.root.origin == "planner" and plan.root.proposed_by is None
        assert _actors(plan, "A") == (None, None, None)
        # And a caller that passes nothing at all (an older desk, a double).
        other = _draft(plans)
        changed, _ = plans.deliver_proposal(
            other.id, other.root_id, _proposal("A"), run_id="run_2", now=2.0
        )
        assert changed.root.proposed_by is None and _actors(changed, "A") == (None, None, None)

    def test_a_regenerated_level_is_the_new_runs_and_what_stays_keeps_its_own(
        self, plans: PlanService
    ) -> None:
        plan = _generated(plans, proposed_by="agent:planner")
        plan = _approve(plans, plan, PAT, "A")
        again, _ = plans.deliver_proposal(
            plan.id,
            plan.root_id,
            PlanProposal(children=[ProposedChild(title="C", kind="code")]),
            run_id="run_2",
            now=6.0,
            proposed_by="agent:architect",
        )
        assert [c.title for c in again.children(again.root_id)] == ["A", "C"]
        assert _actors(again, "A") == ("agent:planner", "usr_pat", None)
        assert _actors(again, "C") == ("agent:architect", None, None)
        assert again.root.proposed_by == "agent:planner", "the root was not generated again"


class TestWhoApproved:
    def test_each_child_approved_carries_its_approver(self, plans: PlanService) -> None:
        plan = _generated(plans)
        plan = _approve(plans, plan, PAT, "A")
        assert _actors(plan, "A") == ("agent:planner", "usr_pat", None)
        assert _actors(plan, "B") == ("agent:planner", None, None)
        plan = _approve(plans, plan, OWNER)
        assert _child(plan, "A").approved_by == "usr_pat", "an approval already given is kept"
        assert _child(plan, "B").approved_by == "usr_owner"

    def test_an_edit_that_makes_it_a_draft_takes_the_approval_with_it(
        self, plans: PlanService
    ) -> None:
        plan = _approve(plans, _generated(plans), PAT)
        child = _child(plan, "A")
        edited = plans.update_node(
            plan.id,
            child.id,
            expected_revision=plan.revision,
            sections={"goal": "sharper"},
            position=None,
            now=6.0,
            actor=OWNER,
        )
        demoted = _child(edited, "A")
        assert demoted.state == "draft"
        assert (demoted.proposed_by, demoted.approved_by) == ("agent:planner", None)
        assert _child(edited, "B").approved_by == "usr_pat"

    def test_a_move_alone_keeps_the_approval(self, plans: PlanService) -> None:
        plan = _approve(plans, _generated(plans), PAT)
        child = _child(plan, "B")
        moved = plans.update_node(
            plan.id,
            child.id,
            expected_revision=plan.revision,
            sections={},
            position=0,
            now=6.0,
            actor=OWNER,
        )
        assert _child(moved, "B").state == "approved"
        assert _child(moved, "B").approved_by == "usr_pat"


class TestWhoPublished:
    def test_every_node_the_level_writes_carries_its_publisher(self, plans: PlanService) -> None:
        plan = _approve(plans, _generated(plans), PAT, "A")
        plan = _publish(plans, plan, FakeGithub(), OWNER)
        assert plan.root.state == "published"
        assert (plan.root.proposed_by, plan.root.approved_by, plan.root.published_by) == (
            "agent:planner",
            None,
            "usr_owner",
        )
        assert _actors(plan, "A") == ("agent:planner", "usr_pat", "usr_owner")
        assert _actors(plan, "B") == ("agent:planner", None, None), "B was not approved"

    def test_a_later_level_does_not_rewrite_who_published_the_first(
        self, plans: PlanService
    ) -> None:
        fake = FakeGithub()
        plan = _approve(plans, _generated(plans), PAT, "A")
        plan = _publish(plans, plan, fake, OWNER)
        plan = _approve(plans, plan, OWNER, "B")
        plan = _publish(plans, plan, fake, PAT)
        assert plan.root.published_by == "usr_owner"
        assert _actors(plan, "A") == ("agent:planner", "usr_pat", "usr_owner")
        assert _actors(plan, "B") == ("agent:planner", "usr_owner", "usr_pat")

    def test_an_issue_attached_from_the_forge_is_nobodys(self, plans: PlanService) -> None:
        fake = FakeGithub()
        plan = _publish(plans, _approve(plans, _generated(plans), PAT), fake, OWNER)
        number = fake.person_files(REPO, "Found work", "## Goal\n\nDo the found work.")
        attached = plans.attach(
            plan.id,
            plan.root_id,
            expected_revision=plan.revision,
            repository=REPO,
            number=number,
            url=None,
            forge_kind="github",
            connect=lambda: fake,
            clock=lambda: 10.0,
            actor=OWNER,
        )
        node = attached.plan.node(attached.node_id)
        assert node is not None and node.origin == "forge"
        assert (node.proposed_by, node.approved_by, node.published_by) == (None, None, None)


class TestARePlan:
    def _published(self, plans: PlanService, fake: FakeGithub) -> Plan:
        return _publish(plans, _approve(plans, _generated(plans), PAT), fake, OWNER)

    def _replan(self, plans: PlanService, plan: Plan, *, proposed_by: str | None) -> Plan:
        replan = PlanReplan.model_validate(
            {
                "add": [{"title": "C", "goal": "C is done", "kind": "code"}],
                "modify": [{"target": _child(plan, "A").id, "goal": "A, but sharper"}],
            }
        )
        kwargs = {} if proposed_by is None else {"proposed_by": proposed_by}
        changed, count = plans.deliver_replan(
            plan.id, plan.root_id, replan, run_id="run_3", now=11.0, **kwargs
        )
        assert count == 2
        return changed

    def _apply(self, plans: PlanService, plan: Plan, fake: FakeGithub) -> Plan:
        applied = plans.approve_replan(
            plan.id,
            plan.root_id,
            expected_revision=plan.revision,
            entry_ids=None,
            forge_kind="github",
            connect=lambda: fake,
            clock=lambda: 12.0,
            actor=PAT,
        )
        assert [r.outcome for r in applied.results] == ["created", "updated"], applied.results
        return applied.plan

    def test_an_addition_is_the_planners_approved_and_published_by_its_approver(
        self, plans: PlanService
    ) -> None:
        fake = FakeGithub()
        plan = self._replan(plans, self._published(plans, fake), proposed_by="agent:architect")
        assert plan.root.replan is not None and plan.root.replan.proposed_by == "agent:architect"
        plan = self._apply(plans, plan, fake)
        assert _actors(plan, "C") == ("agent:architect", "usr_pat", "usr_pat")

    def test_a_change_leaves_who_proposed_approved_and_published_the_child(
        self, plans: PlanService
    ) -> None:
        fake = FakeGithub()
        plan = self._replan(plans, self._published(plans, fake), proposed_by="agent:architect")
        plan = self._apply(plans, plan, fake)
        assert _child(plan, "A").goal == "A, but sharper"
        assert _actors(plan, "A") == ("agent:planner", "usr_pat", "usr_owner")
        assert plan.root.published_by == "usr_owner"

    def test_a_replan_that_names_no_agent_adds_nobodys_child(self, plans: PlanService) -> None:
        fake = FakeGithub()
        plan = self._replan(plans, self._published(plans, fake), proposed_by=None)
        assert plan.root.replan is not None and plan.root.replan.proposed_by is None
        plan = self._apply(plans, plan, fake)
        assert _actors(plan, "C") == (None, "usr_pat", "usr_pat")


class TestTheDesk:
    """The run's desk hands the service the planner bound to its run."""

    def _item(self, plan: Plan, assignment: str | None) -> WorkItem:
        return WorkItem(
            item_id="api:plan:k1",
            source_key="api",
            title="Break it down",
            kind="plan",
            plan_id=plan.id,
            plan_node_id=plan.root_id,
            assignment_json=assignment,
        )

    def _assignment(self, slug: str) -> str:
        binding = AgentBinding(
            slug=slug,
            name=slug.title(),
            role="planner",
            model=None,
            persona="",
            memory_block="",
            tools=None,
            credentials=(),
            revision=1,
        )
        return AgentAssignment(
            lead="lantern", roles={"planner": slug}, agents={slug: binding}
        ).to_json()

    def test_a_proposal_carries_the_runs_planner(self, plans: PlanService) -> None:
        plan = _draft(plans)
        desk = PlanGeneration(plans, self._item(plan, self._assignment("architect")), lambda: 2.0)
        desk.deliver("run_1", _proposal("A"))
        delivered = plans.get(plan.id)
        assert delivered.root.proposed_by == "agent:architect"
        assert _actors(delivered, "A") == ("agent:architect", None, None)

    @pytest.mark.parametrize(
        "assignment",
        [None, '{"roles": {"planner": "architect"}}', "not json"],
        ids=["none", "only-asked-for", "unreadable"],
    )
    def test_an_item_with_no_planned_assignment_records_nobody(
        self, plans: PlanService, assignment: str | None
    ) -> None:
        plan = _draft(plans)
        desk = PlanGeneration(plans, self._item(plan, assignment), lambda: 2.0)
        desk.deliver("run_1", _proposal("A"))
        delivered = plans.get(plan.id)
        assert delivered.root.proposed_by is None
        assert _actors(delivered, "A") == (None, None, None)

    def test_a_replan_carries_the_runs_planner(self, plans: PlanService) -> None:
        fake = FakeGithub()
        plan = _publish(plans, _approve(plans, _generated(plans), PAT), fake, OWNER)
        desk = PlanGeneration(plans, self._item(plan, self._assignment("architect")), lambda: 11.0)
        desk.deliver_replan(
            "run_3", PlanReplan.model_validate({"add": [{"title": "C", "kind": "code"}]})
        )
        waiting = plans.get(plan.id).root.replan
        assert waiting is not None and waiting.proposed_by == "agent:architect"


def test_the_fields_survive_a_write_that_does_not_name_them(plans: PlanService) -> None:
    """A node rewritten for another reason (a move renumbers its siblings)
    keeps who proposed and approved it."""
    plan = _approve(plans, _generated(plans), PAT)
    first = _child(plan, "A")
    stored = plans.store.apply(
        plan.id,
        expected_revision=plan.revision,
        now=7.0,
        upsert=[replace(first, position=3)],
    )
    assert _actors(stored, "A") == ("agent:planner", "usr_pat", None)
