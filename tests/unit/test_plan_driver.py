"""The plan driver: a plan whose ``advance`` is ``auto`` moved forward under
an owner's grants, with every judgement on the decisions ledger.

The loop is the tests' ``Harness`` (real stores); the forge is the fake
GitHub, reached as the daemon's forge for publishing and through the issue
source for an epic run's admissions. A breakdown the driver queues is not
run here: the planner's proposal and the critic's verdict are delivered to
the plan as a reviewed run delivers them (``deliver_proposal``), so each
test controls exactly what the driver sees next. The whole path through the
real engine is ``test_plan_driver_e2e.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lantern.config import Config
from lantern.daemon.controls.delegation import parse_conditions
from lantern.daemon.controls.delegation_store import DecisionRecord
from lantern.daemon.controls.operations import Operation, reconcile_operations
from lantern.daemon.plandriver import PlanDriver
from lantern.daemon.sources import GitHubIssueSource
from lantern.engine.planning import PlanProposal, PlanVerdict
from lantern.errors import LanternError
from lantern.plans.model import Plan
from tests.fakes.fake_github import FakeGithub
from tests.unit.test_daemon_loop import Harness
from tests.unit.test_daemon_sources import LABELS
from tests.unit.test_engine_plan import code_task

PERSON = {"kind": "client", "id": "usr_pat", "display": "Pat", "via": "api"}
DELAY = 900.0
POLL = 60.0


class Box:
    """The daemon's forge sandbox, answered by a fake; ``down`` makes every
    connection fail as an unreachable forge does."""

    kind = "github"

    def __init__(self, ops: Any) -> None:
        self.ops_obj = ops
        self.down = False
        self.failures: list[str] = []

    def ops(self) -> Any:
        if self.down:
            raise LanternError("the forge sandbox is not answering")
        return self.ops_obj

    def call(self, fn: Any) -> Any:
        return fn(self.ops())

    def note_failure(self, exc: BaseException) -> bool:
        self.failures.append(str(exc))
        return False


class World:
    """A daemon with the fake forge, and helpers to drive a plan."""

    def __init__(self, tmp_path: Path, **config: Any) -> None:
        cfg = Config.model_validate(
            {
                "home": str(tmp_path / "state"),
                "github": {"repo": "o/r"},
                "daemon": {
                    "trigger_label": "lantern:run",
                    "in_progress_label": "lantern:in-progress",
                    "poll_interval_s": POLL,
                    **config.pop("daemon", {}),
                },
                **config,
            }
        )
        self.h = Harness(tmp_path, cfg)
        self.fake = FakeGithub()
        self.box = Box(self.fake)
        self.loop.github = self.box
        self.runs = 0

    @property
    def loop(self) -> Any:
        return self.h.loop

    def with_issue_source(self) -> None:
        """An epic run admits its tasks through the issue source."""
        self.loop.source = GitHubIssueSource(
            lambda: self.fake,  # type: ignore[arg-type]
            "o/r",
            LABELS,
            host="db",
        )

    def now(self) -> float:
        return float(self.h.clock())

    def later(self, seconds: float) -> None:
        self.h.clock.t += seconds

    def tick(self) -> None:
        # A hair of time between passes, so the ledger's order is the
        # order things were decided in.
        self.later(0.001)
        self.loop.plan_driver.tick(self.now())

    # -- the owner's grants ----------------------------------------------------

    def grant(
        self, agent: str, action: str, *, daily_limit: int | None = None, **conditions: Any
    ) -> str:
        grant = self.loop.delegation.create_grant(
            agent_slug=agent,
            action=action,
            conditions=parse_conditions(action, conditions),
            daily_limit=daily_limit,
            enabled=True,
            note=None,
            created_by="usr_owner",
            created_by_display="Owner",
            now=self.now(),
        )
        return str(grant.id)

    def grants(self, **approve: Any) -> None:
        """What an owner writes to let a plan go the whole way."""
        self.grant("planner", "plan.breakdown")
        self.grant("critic", "plan.approve", require_review=True, **approve)
        self.grant("critic", "plan.publish", require_review=True)
        self.grant("critic", "plan.run")

    # -- plans -----------------------------------------------------------------

    def plan(self, *, advance: str = "auto", title: str = "Export reports") -> Plan:
        self.later(0.001)  # plans are driven oldest first
        plan = self.loop.plans.create(
            level="epic",
            repository="o/r",
            sections={"title": title, "goal": "download reports as CSV"},
            now=self.now(),
            actor=PERSON,
        )
        if advance == "auto":
            plan = self.loop.plans.update(
                plan.id,
                expected_revision=plan.revision,
                sections={},
                now=self.now(),
                actor=PERSON,
                advance="auto",
            )
        return plan

    def get(self, plan: Plan | str) -> Plan:
        plan_id = plan if isinstance(plan, str) else plan.id
        got: Plan = self.loop.plans.get(plan_id)
        return got

    def flip(self, plan: Plan, advance: str) -> Plan:
        now = self.get(plan)
        flipped: Plan = self.loop.plans.update(
            now.id,
            expected_revision=now.revision,
            sections={},
            now=self.now(),
            actor=PERSON,
            advance=advance,
        )
        return flipped

    def breakdowns(self, plan: Plan | None = None) -> list[Any]:
        return [
            i
            for i in self.h.dstore.items()
            if i.kind == "plan" and (plan is None or i.plan_id == plan.id)
        ]

    def deliver(
        self,
        plan: Plan,
        *,
        verdict: str | None = "approve",
        proposed_by: str | None = "agent:planner",
        children: int = 2,
    ) -> Plan:
        """What a reviewed breakdown run delivers, and its item done."""
        (item,) = [i for i in self.breakdowns(plan) if i.state == "queued"]
        tasks = [code_task("c1")] + [
            code_task(f"c{n}", deps=[f"c{n - 1}"]) for n in range(2, children + 1)
        ]
        proposal = PlanProposal.model_validate(
            {
                "root": {
                    "title": "Export reports",
                    "goal": "Download reports as CSV",
                    "context": "The reports module handles exports",
                    "acceptance_criteria": ["Reports export as CSV"],
                },
                "children": tasks,
            }
        )
        self.runs += 1
        self.loop.plans.deliver_proposal(
            plan.id,
            plan.root_id,
            proposal,
            run_id=f"r{self.runs}",
            now=self.now(),
            item_id=item.item_id,
            proposed_by=proposed_by,
            review=None
            if verdict is None
            else PlanVerdict(
                verdict=verdict,  # type: ignore[arg-type]
                reasons=[] if verdict == "approve" else ["Task c2 cannot be checked alone."],
            ),
            reviewed_by=None if verdict is None else "agent:critic",
        )
        self.h.dstore.set_state(item.item_id, "done", self.now())
        return self.get(plan)

    # -- what was recorded -----------------------------------------------------

    def decisions(self, plan: Plan | None = None) -> list[DecisionRecord]:
        rows = self.loop.delegation.page(limit=500)
        return sorted(
            (r for r in rows if plan is None or r.plan_id == plan.id),
            key=lambda r: (r.at, r.id),
        )

    def operations(self) -> list[Operation]:
        ops: list[Operation] = self.loop.operations.recent(limit=500)
        return sorted(ops, key=lambda o: (o.accepted_at, o.id))

    def states(self, plan: Plan) -> list[str]:
        return [c.state for c in self.get(plan).children(plan.root_id)]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _approved(w: World, plan: Plan, **delivered: Any) -> Plan:
    """A plan broken down, reviewed and approved by the driver."""
    w.tick()
    w.deliver(plan, **delivered)
    w.tick()
    assert set(w.states(plan)) == {"approved"}
    return w.get(plan)


class TestTheWholeWay:
    def test_a_plan_goes_from_brief_to_running_epic_with_no_person(self, world: World) -> None:
        w = world
        w.with_issue_source()
        w.grants()
        plan = w.plan()

        # 1. The breakdown is admitted, as the planner.
        w.tick()
        (item,) = w.breakdowns(plan)
        assert item.state == "queued" and item.plan_node_id == plan.root_id
        # Its agents are bound without memories: an owner's grant acts on it.
        assert item.assignment_json is not None and '"memoryless": true' in item.assignment_json
        w.tick()
        assert len(w.breakdowns(plan)) == 1, "a breakdown under way is never queued twice"

        # 2. The run proposes and the critic approves; the driver approves.
        w.deliver(plan)
        w.tick()
        approved = w.get(plan)
        assert w.states(plan) == ["approved", "approved"]
        for child in approved.children(plan.root_id):
            assert child.approved_by == "agent:critic"
            assert child.proposed_by == "agent:planner"

        # 3. Not published inside the window; published once it has passed.
        w.later(DELAY - 1)
        w.tick()
        assert w.states(plan) == ["approved", "approved"]
        w.later(1)
        w.tick()
        published = w.get(plan)
        assert published.root.state == "published"
        assert w.states(plan) == ["published", "published"]
        assert published.root.published_by == "agent:critic"
        for child in published.children(plan.root_id):
            assert child.published_by == "agent:critic"

        # 4. The epic run starts, as the critic, and admits what is ready.
        w.tick()
        run = w.loop.epic_runs.runs.latest(plan.id, plan.root_id)
        assert run is not None and run.state == "running"
        assert run.started_by == "agent:critic"
        assert run.started_by_display == "the critic agent"
        assert [t.state for t in run.tasks] == ["queued", "waiting"]

        # Nothing more to do: later ticks write nothing.
        before = (len(w.decisions()), len(w.operations()))
        w.later(3600)
        w.tick()
        w.tick()
        assert (len(w.decisions()), len(w.operations())) == before

        # The ledger: four allows, each under its grant, nothing escalated.
        rows = w.decisions(plan)
        assert [(r.agent_slug, r.action, r.outcome) for r in rows] == [
            ("planner", "plan.breakdown", "allow"),
            ("critic", "plan.approve", "allow"),
            ("critic", "plan.publish", "allow"),
            ("critic", "plan.run", "allow"),
        ]
        assert all(r.grant_id and r.operation_id for r in rows)
        assert w.loop.delegation.unresolved_count() == 0
        assert rows[0].item_id == item.item_id
        assert rows[3].epic_run_id == run.id
        approve = rows[1].attrs
        assert approve["repository"] == "o/r" and approve["level"] == "task"
        assert approve["child_count"] == 2 and approve["proposer"] == "agent:planner"
        assert approve["review_verdict"] == "approve"
        assert rows[3].attrs == {"repository": "o/r", "level": "task", "child_count": 2}

        # The operations log: each act under the agent that took it.
        ops = {
            o.id: o
            for o in w.operations()
            if o.action in ("item.admit", "plan.approve", "plan.publish", "plan.run")
        }
        assert [(ops[r.operation_id].action, ops[r.operation_id].state) for r in rows] == [  # type: ignore[index]
            ("item.admit", "succeeded"),
            ("plan.approve", "succeeded"),
            ("plan.publish", "succeeded"),
            ("plan.run", "succeeded"),
        ]
        actors = [ops[r.operation_id].actor for r in rows]  # type: ignore[index]
        assert [(a["kind"], a["id"]) for a in actors] == [
            ("agent", "agent:planner"),
            ("agent", "agent:critic"),
            ("agent", "agent:critic"),
            ("agent", "agent:critic"),
        ]


class TestNothingHappens:
    def test_without_a_grant_nothing_is_read_or_written(self, world: World) -> None:
        plan = world.plan()
        revision = plan.revision
        for _ in range(3):
            world.tick()
            world.later(DELAY)
        assert world.breakdowns() == []
        assert world.decisions() == []
        assert world.operations() == []
        assert world.get(plan).revision == revision

    def test_a_disabled_grant_is_no_grant(self, world: World) -> None:
        grant = world.loop.delegation.grant(world.grant("planner", "plan.breakdown"))
        world.loop.delegation.update_grant(
            grant.id, {"enabled": False}, expected_revision=grant.revision, now=world.now()
        )
        world.plan()
        world.tick()
        assert world.breakdowns() == [] and world.decisions() == []

    def test_a_manual_plan_is_never_touched(self, world: World) -> None:
        world.grants()
        plan = world.plan(advance="manual")
        world.tick()
        assert world.breakdowns() == [] and world.decisions() == []
        assert world.get(plan).revision == plan.revision

    def test_a_held_daemon_moves_no_plan(self, world: World) -> None:
        world.grants()
        world.plan()
        # The loop's own tick dispatches what is queued; this test's plan
        # runs need no forge.
        world.loop.github = None
        world.loop.pause(by="Pat", via="test")
        world.loop.tick()
        assert world.breakdowns() == [] and world.decisions() == []
        world.loop.unpause(by="Pat")
        # The loop's own tick drives it once it is released.
        world.later(1)
        world.loop.tick()
        assert len(world.breakdowns()) == 1
        (row,) = world.decisions()
        assert (row.action, row.outcome) == ("plan.breakdown", "allow")


class TestEscalations:
    def test_a_level_over_max_children_escalates_once_and_resolves_when_a_person_approves(
        self, world: World
    ) -> None:
        w = world
        w.grants(max_children=1)
        plan = w.plan()
        w.tick()
        w.deliver(plan)
        for _ in range(3):
            w.tick()
            w.later(POLL)
        rows = [r for r in w.decisions(plan) if r.action == "plan.approve"]
        assert len(rows) == 1, "the same escalation is written once"
        (escalation,) = rows
        assert escalation.outcome == "escalate" and escalation.unresolved
        assert "max_children is 1 and child_count is 2" in escalation.reason
        assert w.states(plan) == ["proposed", "proposed"]
        # A person approves the level themselves.
        now = w.get(plan)
        w.loop.plans.approve(
            plan.id,
            plan.root_id,
            expected_revision=now.revision,
            node_ids=None,
            now=w.now(),
            actor=PERSON,
        )
        w.tick()
        resolved = w.loop.delegation.decision(escalation.id)
        assert resolved is not None
        assert (resolved.resolution, resolved.resolved_by) == ("acted", "usr_pat")

    def test_a_reviewer_who_escalates_stops_the_approval(self, world: World) -> None:
        world.grants()
        plan = world.plan()
        world.tick()
        world.deliver(plan, verdict="escalate")
        world.tick()
        world.tick()
        assert world.states(plan) == ["proposed", "proposed"]
        (row,) = [r for r in world.decisions(plan) if r.action == "plan.approve"]
        assert row.outcome == "escalate"
        assert row.reason.startswith("the reviewer escalated this level")
        assert row.attrs["review_verdict"] == "escalate"

    def test_without_a_review_a_grant_that_asks_for_one_escalates(self, world: World) -> None:
        world.grants()
        plan = world.plan()
        world.tick()
        world.deliver(plan, verdict=None)
        world.tick()
        assert world.states(plan) == ["proposed", "proposed"]
        (row,) = [r for r in world.decisions(plan) if r.action == "plan.approve"]
        assert row.outcome == "escalate" and "review_verdict" not in row.attrs
        assert "it needs review_verdict, and it was not supplied" in row.reason

    def test_an_edit_in_the_hold_window_stops_the_publish(self, world: World) -> None:
        w = world
        w.grants()
        plan = _approved(w, w.plan())
        child = w.get(plan).children(plan.root_id)[1]
        w.later(60)
        w.loop.plans.update_node(
            plan.id,
            child.id,
            expected_revision=w.get(plan).revision,
            sections={"goal": "something else"},
            position=None,
            now=w.now(),
            actor=PERSON,
        )
        w.later(DELAY)
        w.tick()
        w.tick()
        assert w.get(plan).root.state != "published"
        assert [o for o in w.operations() if o.action == "plan.publish"] == []
        approvals = [r for r in w.decisions(plan) if r.action == "plan.approve"]
        assert [r.outcome for r in approvals] == ["allow", "escalate"]
        assert "review_verdict" not in approvals[1].attrs, "the review no longer reads current"

    def test_a_breakdown_that_failed_goes_to_a_person_and_is_not_queued_again(
        self, world: World
    ) -> None:
        world.grants()
        plan = world.plan()
        world.tick()
        (item,) = world.breakdowns(plan)
        world.h.dstore.set_state(item.item_id, "failed", world.now())
        for _ in range(3):
            world.tick()
            world.later(POLL)
        assert len(world.breakdowns(plan)) == 1
        rows = world.decisions(plan)
        assert [(r.action, r.outcome) for r in rows] == [
            ("plan.breakdown", "allow"),
            ("plan.breakdown", "escalate"),
        ]
        assert item.item_id in rows[1].reason and "failed" in rows[1].reason

    def test_a_repository_planning_is_off_for_escalates(self, tmp_path: Path) -> None:
        w = World(tmp_path)
        w.grants()
        plan = w.plan()
        w.loop.config = Config.model_validate(
            {**w.loop.config.model_dump(mode="json"), "planning": {"enabled": False}}
        )
        w.tick()
        w.tick()
        assert w.breakdowns() == []
        (row,) = w.decisions(plan)
        assert row.outcome == "escalate" and "planning is off" in row.reason


class TestNoSelfApproval:
    def test_a_level_the_critic_proposed_is_denied_once(self, world: World) -> None:
        world.grants()
        plan = world.plan()
        world.tick()
        world.deliver(plan, proposed_by="agent:critic")
        world.tick()
        world.tick()
        (row,) = [r for r in world.decisions(plan) if r.action == "plan.approve"]
        assert row.outcome == "deny" and "never approves its own" in row.reason
        assert world.states(plan) == ["proposed", "proposed"]

    def test_a_level_nobody_is_named_as_proposing_escalates(self, world: World) -> None:
        world.grants()
        plan = world.plan()
        world.tick()
        world.deliver(plan, proposed_by=None)
        world.tick()
        (row,) = [r for r in world.decisions(plan) if r.action == "plan.approve"]
        assert row.outcome == "escalate" and "could not tell who proposed" in row.reason
        assert row.attrs["proposers"] == [None]


class TestDailyLimit:
    def test_a_spent_limit_escalates_and_the_next_cap_day_allows(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.breakdown")
        w.grant("critic", "plan.approve", daily_limit=1)
        first, second = w.plan(title="First"), w.plan(title="Second")
        w.tick()
        w.deliver(first)
        w.deliver(second)
        w.tick()
        assert set(w.states(first)) == {"approved"}
        assert set(w.states(second)) == {"proposed"}
        (spent,) = [r for r in w.decisions(second) if r.action == "plan.approve"]
        assert spent.outcome == "escalate" and "daily_limit of 1 is spent" in spent.reason
        w.tick()
        assert len([r for r in w.decisions(second) if r.action == "plan.approve"]) == 1
        w.later(86400)
        w.tick()
        assert set(w.states(second)) == {"approved"}
        resolved = w.loop.delegation.decision(spent.id)
        assert resolved is not None and resolved.resolution == "acted"
        assert resolved.resolved_by == "agent:critic"


class TestAPersonTakesItBack:
    def test_flipping_to_manual_stops_the_driver_before_the_next_step(self, world: World) -> None:
        w = world
        w.grants()
        plan = _approved(w, w.plan())
        decided = len(w.decisions())
        w.flip(plan, "manual")
        w.later(DELAY * 4)
        w.tick()
        w.tick()
        assert w.get(plan).root.state != "published"
        assert len(w.decisions()) == decided
        # Back to auto: it takes up where it stood.
        w.flip(plan, "auto")
        w.tick()
        assert w.get(plan).root.state == "published"


class TestOneForgeWritePerTick:
    def test_two_plans_due_to_publish_are_published_one_tick_apart(self, world: World) -> None:
        w = world
        w.grants()
        first, second = w.plan(title="First"), w.plan(title="Second")
        w.tick()
        w.deliver(first)
        w.deliver(second)
        w.tick()
        assert set(w.states(first)) == set(w.states(second)) == {"approved"}
        w.later(DELAY)
        w.tick()
        published = [p for p in (first, second) if w.get(p).root.state == "published"]
        assert len(published) == 1
        w.tick()
        assert w.get(first).root.state == w.get(second).root.state == "published"


class TestRestart:
    def test_the_hold_window_and_the_ledger_survive_a_restart(self, world: World) -> None:
        w = world
        w.grants()
        plan = w.plan()
        w.tick()
        # A new driver (a restart) finds the breakdown under way.
        w.loop.plan_driver = PlanDriver(w.loop)
        w.tick()
        assert len(w.breakdowns(plan)) == 1
        w.deliver(plan)
        w.tick()
        w.later(DELAY / 2)
        w.loop.plan_driver = PlanDriver(w.loop)
        w.tick()
        assert w.get(plan).root.state != "published", "the window is the plan's, not memory's"
        w.later(DELAY / 2)
        w.loop.plan_driver = PlanDriver(w.loop)
        w.tick()
        assert w.get(plan).root.state == "published"
        actions = [r.action for r in w.decisions(plan)]
        assert actions == ["plan.breakdown", "plan.approve", "plan.publish"]

    def test_an_escalation_is_not_written_again_after_a_restart(self, world: World) -> None:
        world.grant("planner", "plan.breakdown")
        plan = world.plan()
        world.tick()
        world.deliver(plan)
        world.tick()
        world.loop.plan_driver = PlanDriver(world.loop)
        world.tick()
        rows = [r for r in world.decisions(plan) if r.action == "plan.approve"]
        assert [r.outcome for r in rows] == ["escalate"]
        assert "no enabled grant lets critic take plan.approve" in rows[0].reason


class TestForgeErrors:
    def test_a_forge_error_escalates_once_and_is_not_tried_every_tick(self, world: World) -> None:
        w = world
        w.grants()
        plan = _approved(w, w.plan())
        w.later(DELAY)
        w.box.down = True
        w.tick()
        publishes = [o for o in w.operations() if o.action == "plan.publish"]
        assert [o.state for o in publishes] == ["failed"]
        (failed,) = [r for r in w.decisions(plan) if r.action == "plan.publish"]
        assert failed.outcome == "escalate" and failed.operation_id == publishes[0].id
        assert "could not reach the forge" in failed.reason
        # The next ticks inside the poll interval do not try again.
        w.tick()
        w.later(POLL - 1)
        w.tick()
        assert len([o for o in w.operations() if o.action == "plan.publish"]) == 1
        # After it, one more try; still down, nothing new on the ledger.
        w.later(1)
        w.tick()
        assert len([o for o in w.operations() if o.action == "plan.publish"]) == 2
        assert len([r for r in w.decisions(plan) if r.action == "plan.publish"]) == 1
        # The forge is back: published, and the escalation is closed.
        w.box.down = False
        w.later(POLL)
        w.tick()
        assert w.get(plan).root.state == "published"
        rows = [r for r in w.decisions(plan) if r.action == "plan.publish"]
        assert [r.outcome for r in rows] == ["escalate", "allow"]
        closed = w.loop.delegation.decision(failed.id)
        assert closed is not None and closed.resolution == "acted"

    def test_no_forge_write_while_the_forge_is_not_provisioned(self, world: World) -> None:
        w = world
        w.grants()
        plan = _approved(w, w.plan())
        w.box.provisioned = False  # type: ignore[attr-defined]
        w.later(DELAY)
        w.tick()
        assert w.get(plan).root.state != "published"
        assert [r for r in w.decisions(plan) if r.action == "plan.publish"] == []
        w.box.provisioned = True  # type: ignore[attr-defined]
        w.tick()
        assert w.get(plan).root.state == "published"


class TestAnInitiative:
    def test_each_published_epic_is_broken_down_in_turn(self, world: World) -> None:
        w = world
        w.grants()
        plan = w.loop.plans.create(
            level="initiative",
            repository="o/r",
            sections={"title": "Reports", "goal": "reports people can use"},
            now=w.now(),
            actor=PERSON,
        )
        plan = w.loop.plans.update(
            plan.id,
            expected_revision=plan.revision,
            sections={},
            now=w.now(),
            actor=PERSON,
            advance="auto",
        )
        w.tick()
        (item,) = w.breakdowns(plan)
        proposal = PlanProposal.model_validate(
            {
                "root": {
                    "title": "Reports",
                    "goal": "Reports people can use",
                    "context": "the reports module",
                    "acceptance_criteria": ["reports are usable"],
                },
                "children": [
                    {
                        "id": f"e{n}",
                        "title": f"Epic {n}",
                        "goal": f"epic {n} works",
                        "context": "the reports module",
                        "acceptance_criteria": [f"epic {n} is done"],
                    }
                    for n in (1, 2)
                ],
            }
        )
        w.loop.plans.deliver_proposal(
            plan.id,
            plan.root_id,
            proposal,
            run_id="r1",
            now=w.now(),
            item_id=item.item_id,
            proposed_by="agent:planner",
            review=PlanVerdict(verdict="approve", reasons=[]),
            reviewed_by="agent:critic",
        )
        w.h.dstore.set_state(item.item_id, "done", w.now())
        w.tick()
        w.later(DELAY)
        w.tick()
        epics = w.get(plan).children(plan.root_id)
        assert [e.state for e in epics] == ["published", "published"]
        # One breakdown per tick, the plan's order.
        w.tick()
        assert [i.plan_node_id for i in w.breakdowns(plan) if i.state == "queued"] == [epics[0].id]
        w.tick()
        assert [i.plan_node_id for i in w.breakdowns(plan) if i.state == "queued"] == [
            epics[0].id,
            epics[1].id,
        ]
        breakdowns = [r for r in w.decisions(plan) if r.action == "plan.breakdown"]
        assert [r.attrs["level"] for r in breakdowns] == ["epic", "task", "task"]


class TestReconciled:
    def test_a_driver_breakdown_cut_short_is_settled_from_the_queue(self, world: World) -> None:
        world.grants()
        plan = world.plan()
        world.tick()
        (op,) = [o for o in world.operations() if o.action == "item.admit"]
        assert op.request["form"] == "plan" and op.state == "succeeded"
        # The same record, left claimed by a generation that died.
        again, _ = world.loop.operations.accept(_spec_like(op, "the same item"), now=world.now())
        world.loop.operations.claim(again.id, "g_dead", world.now())
        gone, _ = world.loop.operations.accept(_spec_like(op, None), now=world.now())
        world.loop.operations.claim(gone.id, "g_dead", world.now())
        reconcile_operations(world.loop, generation="g_new", now=world.now())
        settled = world.loop.operations.get(again.id)
        lost = world.loop.operations.get(gone.id)
        assert settled is not None and settled.state == "succeeded"
        assert lost is not None and (lost.state, lost.error_code) == (
            "failed",
            "interrupted_before_effect",
        )
        assert plan.id


def _spec_like(op: Operation, item: str | None) -> Any:
    from lantern.daemon.controls.operations import OperationSpec
    from lantern.daemon.controls.principal import Principal

    return OperationSpec(
        action="item.admit",
        target_kind="item",
        target_key=op.target_key if item else "api:plan:never-queued",
        principal=Principal.for_agent("planner"),
        request=dict(op.request),
    )
