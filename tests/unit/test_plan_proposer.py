"""Proposing: the planner drafting an ``auto`` plan from an owner's goal
under a ``plan.propose`` grant, at most once per ``[delegation]
propose_every``, and the loop guard that keeps propose → run → follow-up →
propose from running on.

The daemon is the plan driver's test world (real stores, the fake GitHub as
the daemon's forge); a breakdown is delivered to the plan as a reviewed run
delivers it (``World.deliver``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lantern.agents.assignment import AgentAssignment
from lantern.agents.origin import WorkOrigin, origin_from_body, origin_marker
from lantern.api.routes.meta import features
from lantern.config import Config
from lantern.daemon.controls.operations import OperationSpec, reconcile_operations
from lantern.daemon.controls.principal import Principal
from lantern.daemon.goals import Goal
from lantern.daemon.plandriver import PlanDriver
from lantern.daemon.planproposer import (
    INITIATIVE_TEXT_CHARS,
    MAX_FOLLOWUPS,
    brief,
    plan_depth,
    root_level,
)
from lantern.engine.followups import Candidate, issue_body, origin_for_run
from lantern.engine.review import Followup as ReviewFollowup
from lantern.plans.model import Plan
from tests.unit.test_plan_driver import DELAY, PERSON, World

EVERY = 86_400.0
LABEL = "lantern:follow-up"


def _world(tmp_path: Path, *, every: float = EVERY, **config: Any) -> World:
    delegation = {"publish_delay_s": int(DELAY), "propose_every": int(every)}
    return World(tmp_path, delegation=delegation, **config)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return _world(tmp_path)


def _goal(
    w: World, title: str = "Faster exports", text: str = "Exports finish in seconds."
) -> Goal:
    w.later(0.001)
    goal: Goal = w.loop.goals.create(
        repository="o/r",
        title=title,
        text=text,
        created_by="usr_owner",
        created_by_display="Owner",
        now=w.now(),
    )
    return goal


def _set_goal(w: World, goal: Goal, **changes: Any) -> Goal:
    current = w.loop.goals.goal(goal.id)
    updated: Goal = w.loop.goals.update(
        goal.id, changes, expected_revision=current.revision, now=w.now()
    )
    return updated


def _plans(w: World, goal: Goal) -> list[Plan]:
    return [w.get(p.plan_id) for p in w.loop.goals.plans_for(goal.id)]


def _proposals(w: World, outcome: str | None = None) -> list[Any]:
    return [
        r
        for r in w.decisions()
        if r.action == "plan.propose" and (outcome is None or r.outcome == outcome)
    ]


def _followup(w: World, title: str, *, depth: int | None = None, label: str = LABEL) -> int:
    body = f"{title}, noted by the review."
    if depth is not None:
        body += "\n" + origin_marker(WorkOrigin(agent_slug="planner", chain_depth=depth))
    ref = w.fake.issue_create("o/r", title, body, labels=[label])
    return int(ref.number)


def _archive(w: World, plan: Plan) -> None:
    current = w.get(plan)
    w.loop.plans.delete(plan.id, expected_revision=current.revision, now=w.now(), actor=PERSON)


class TestOff:
    def test_off_by_default_nothing_is_read_or_written(self, tmp_path: Path) -> None:
        w = World(tmp_path)
        assert w.loop.config.delegation.propose_every == 0
        w.grant("planner", "plan.propose")
        _goal(w)
        goal = _goal(w)
        _followup(w, "Retry the export")
        w.box.down = True  # any forge read would fail, and be recorded
        for _ in range(3):
            w.later(EVERY)
            w.tick()
        assert _plans(w, goal) == []
        assert _proposals(w) == []
        assert w.operations() == []
        assert w.box.failures == []


class TestJudging:
    def test_without_a_grant_one_escalation_per_goal_never_repeated(self, world: World) -> None:
        w = world
        first, second = _goal(w, "One"), _goal(w, "Two")
        for _ in range(3):
            w.tick()
        w.loop.plan_driver = PlanDriver(w.loop)  # a restart
        w.later(EVERY * 2)
        w.tick()
        rows = _proposals(w)
        assert sorted((r.attrs["goal_id"], r.outcome) for r in rows) == sorted(
            [(first.id, "escalate"), (second.id, "escalate")]
        )
        rows.sort(key=lambda r: r.attrs["goal_id"] != first.id)
        assert all("no enabled grant lets planner take plan.propose" in r.reason for r in rows)
        assert all(r.unresolved and r.plan_id is None for r in rows)
        assert rows[0].attrs == {"repository": "o/r", "level": "epic", "goal_id": first.id}
        assert _plans(w, first) == [] and _plans(w, second) == []
        assert w.operations() == []

    def test_a_grant_for_other_repositories_escalates_naming_it(self, world: World) -> None:
        world.grant("planner", "plan.propose", repositories=["o/other"])
        _goal(world)
        world.tick()
        (row,) = _proposals(world)
        assert row.outcome == "escalate" and "repositories" in row.reason

    def test_a_disabled_repository_escalates_before_any_grant(self, tmp_path: Path) -> None:
        w = _world(tmp_path, github={"repos": [{"repo": "o/r", "enabled": False}]})
        w.grant("planner", "plan.propose")
        _goal(w)
        w.tick()
        w.tick()
        (row,) = _proposals(w)
        assert row.outcome == "escalate" and "o/r is disabled on this server" in row.reason
        assert w.fake.issues_created == []

    def test_paused_and_done_goals_are_never_proposed_for(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose")
        _set_goal(w, _goal(w, "Paused"), state="paused")
        _set_goal(w, _goal(w, "Done"), state="done")
        w.tick()
        assert _proposals(w) == []

    def test_an_escalation_is_superseded_when_the_goal_is_paused(self, world: World) -> None:
        w = world
        goal = _goal(w)
        w.tick()
        (row,) = _proposals(w)
        assert row.unresolved
        _set_goal(w, goal, state="paused")
        w.tick()
        (row,) = _proposals(w)
        assert row.resolution == "superseded"

    def test_the_escalation_is_closed_as_acted_when_the_grant_comes(self, world: World) -> None:
        w = world
        goal = _goal(w)
        w.tick()
        w.grant("planner", "plan.propose")
        w.tick()
        rows = _proposals(w)
        assert [r.outcome for r in rows] == ["escalate", "allow"]
        assert rows[0].resolution == "acted" and rows[0].resolved_by == "agent:planner"
        assert len(_plans(w, goal)) == 1


class TestProposing:
    def test_an_allowed_proposal_drafts_one_auto_plan_with_the_brief(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose", levels=["epic"])
        goal = _goal(w)
        old = _followup(w, "Retry a failed export")
        new = _followup(w, "Name the export file after the report")
        _followup(w, "Not a follow-up", label="bug")
        w.tick()

        (plan,) = _plans(w, goal)
        assert plan.created_by == "agent:planner" and plan.advance == "auto"
        assert plan.goal_id == goal.id and plan.root.level == "epic"
        assert plan.root.proposed_by == "agent:planner"
        assert plan.generation_pending, "the root is generated from the brief"
        assert plan.input["title"] == "Faster exports"
        assert plan.input["goal"] == "Exports finish in seconds."
        context = plan.input["context"]
        assert "Not a follow-up" not in context
        lines = [line for line in context.splitlines() if line.startswith("- ")]
        assert lines == [
            f"- Name the export file after the report (https://github.com/o/r/issues/{new})",
            f"- Retry a failed export (https://github.com/o/r/issues/{old})",
        ]

        (row,) = _proposals(w)
        assert row.outcome == "allow" and row.agent_slug == "planner"
        assert row.plan_id == plan.id and row.node_id == plan.root_id
        assert row.attrs["chain_depth"] == 1 and row.attrs["followups"] == [new, old]
        (op,) = [o for o in w.operations() if o.action == "plan.propose"]
        assert op.id == row.operation_id and op.state == "succeeded"
        assert op.actor["id"] == "agent:planner" and op.actor["kind"] == "agent"
        assert op.target_kind == "plan" and op.target_key == plan.id

    def test_the_driver_then_carries_the_proposed_plan(self, world: World) -> None:
        w = world
        w.with_issue_source()
        w.grant("planner", "plan.propose")
        w.grants()
        goal = _goal(w)
        # Proposed, and in the same pass the driver admits its root
        # generation as the planner.
        w.tick()
        (plan,) = _plans(w, goal)
        (item,) = w.breakdowns(plan)
        assert item.state == "queued" and item.plan_node_id == plan.root_id
        w.deliver(plan)
        w.tick()
        assert set(w.states(plan)) == {"approved"}
        w.later(DELAY)
        w.tick()
        assert w.get(plan).root.state == "published"
        w.tick()
        run = w.loop.epic_runs.runs.latest(plan.id, plan.root_id)
        assert run is not None and run.started_by == "agent:critic"
        # The tasks it admits are the planner's work, one hop deep.
        admitted = [w.h.dstore.get(t.item_id) for t in run.tasks if t.item_id]
        assert admitted and all(
            i.origin_agent == "planner" and i.chain_depth == 1 for i in admitted
        )
        # The proposal and the breakdown share a tick: ordered by the act.
        order = ["plan.propose", "plan.breakdown"]
        rows = sorted(
            w.decisions(), key=lambda r: (r.at, order.index(r.action) if r.action in order else 2)
        )
        actions = [(r.agent_slug, r.action, r.outcome) for r in rows]
        assert actions == [
            ("planner", "plan.propose", "allow"),
            ("planner", "plan.breakdown", "allow"),
            ("critic", "plan.approve", "allow"),
            ("critic", "plan.publish", "allow"),
            ("critic", "plan.run", "allow"),
        ]
        people = [o for o in w.operations() if o.actor.get("kind") != "agent"]
        assert people == []

    def test_one_open_plan_per_goal_and_one_proposal_per_tick(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose")
        first, second = _goal(w, "One"), _goal(w, "Two")
        w.tick()
        assert (len(_plans(w, first)), len(_plans(w, second))) == (1, 0)
        w.tick()
        assert (len(_plans(w, first)), len(_plans(w, second))) == (1, 1)
        for _ in range(3):
            w.later(EVERY)
            w.tick()
        assert (len(_plans(w, first)), len(_plans(w, second))) == (1, 1)
        assert len(_proposals(w, "allow")) == 2

    def test_a_daily_limit_bounds_the_proposals(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose", daily_limit=1)
        first, second = _goal(w, "One"), _goal(w, "Two")
        w.tick()
        w.tick()
        assert (len(_plans(w, first)), len(_plans(w, second))) == (1, 0)
        (row,) = _proposals(w, "escalate")
        assert "daily_limit of 1 is spent" in row.reason

    def test_an_initiative_roots_a_long_goal(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose", levels=["epic"])
        long = _goal(w, "Broad", "x" * INITIATIVE_TEXT_CHARS)
        w.tick()
        (row,) = _proposals(w)
        assert row.outcome == "escalate" and row.attrs["level"] == "initiative"
        w.grant("planner", "plan.propose", levels=["initiative"])
        w.tick()
        (plan,) = _plans(w, long)
        assert plan.root.level == "initiative"

    def test_a_forge_that_cannot_be_read_proposes_nothing_and_backs_off(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose")
        goal = _goal(w)
        w.box.down = True
        w.tick()
        w.tick()
        (row,) = _proposals(w)
        assert row.outcome == "escalate" and "could not be read" in row.reason
        assert _plans(w, goal) == []
        w.box.down = False
        w.tick()
        assert _plans(w, goal) == [], "the failed attempt waits before it is tried again"
        w.later(60)
        w.tick()
        assert len(_plans(w, goal)) == 1
        assert _proposals(w)[0].resolution == "acted"


class TestCadence:
    def test_the_period_is_kept_across_a_restart_after_a_plan_is_archived(
        self, world: World
    ) -> None:
        w = world
        w.grant("planner", "plan.propose")
        goal = _goal(w)
        w.tick()
        (plan,) = _plans(w, goal)
        _archive(w, plan)  # a draft: deleted outright
        w.later(EVERY / 2)
        w.tick()
        w.loop.plan_driver = PlanDriver(w.loop)  # a restart
        w.later(EVERY / 2 - 1)
        w.tick()
        assert _plans(w, goal) == []
        w.later(1)
        w.tick()
        assert len(_plans(w, goal)) == 1
        assert len(_proposals(w, "allow")) == 2

    def test_a_finished_plan_is_followed_a_period_later_and_the_goal_stays_active(
        self, world: World
    ) -> None:
        w = world
        w.with_issue_source()
        w.grant("planner", "plan.propose")
        w.grants()
        goal = _goal(w)
        w.tick()
        (plan,) = _plans(w, goal)
        w.tick()
        w.deliver(plan)
        w.tick()
        w.later(DELAY)
        w.tick()
        published = w.get(plan)
        assert published.root.forge is not None
        # The epic is closed on the forge and read back: the plan is done.
        w.fake.person_edits("o/r", published.root.forge.number, state="closed")
        w.loop.plans.reconcile(plan.id, actor=PERSON, force=True, **_forge(w))
        assert w.get(plan).root.forge.state == "closed"  # type: ignore[union-attr]
        done_at = w.get(plan).updated_at
        w.h.clock.t = done_at + EVERY - 1
        w.tick()
        assert len(_plans(w, goal)) == 1
        w.h.clock.t = done_at + EVERY
        w.tick()
        assert len(_plans(w, goal)) == 2
        assert w.loop.goals.goal(goal.id).state == "active", "never marked done for the owner"


def _forge(w: World) -> dict[str, Any]:
    return {"connect": w.box.ops, "forge_kind": "github", "clock": w.loop.clock}


class TestFollowupDepth:
    def test_follow_ups_at_or_beyond_the_chain_ceiling_are_left_out(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose")
        goal = _goal(w)
        person = _followup(w, "From a person's run")
        one = _followup(w, "One hop", depth=1)
        _followup(w, "Two hops", depth=2)
        _followup(w, "Three hops", depth=3)
        w.tick()
        (plan,) = _plans(w, goal)
        context = plan.input["context"]
        assert "One hop" in context and "From a person's run" in context
        assert "Two hops" not in context and "Three hops" not in context
        (row,) = _proposals(w, "allow")
        assert row.attrs["followups"] == [one, person]
        assert row.attrs["chain_depth"] == 2

    def test_at_most_ten_follow_ups_newest_first(self, world: World) -> None:
        w = world
        w.grant("planner", "plan.propose")
        goal = _goal(w)
        numbers = [_followup(w, f"Note {n}") for n in range(MAX_FOLLOWUPS + 3)]
        w.fake.issue_create("o/r", "Closed", "", labels=[LABEL])
        w.fake.existing_issues[-1]["state"] = "closed"
        w.fake.existing_issues.append(
            {
                "number": 5000,
                "title": "A PR",
                "html_url": "u",
                "labels": [{"name": LABEL}],
                "pull_request": {},
            }
        )
        w.tick()
        (row,) = _proposals(w, "allow")
        assert row.attrs["followups"] == sorted(numbers, reverse=True)[:MAX_FOLLOWUPS]
        assert len(_plans(w, goal)) == 1

    def test_the_root_level_and_the_brief_are_pure(self) -> None:
        goal = Goal(
            id="goal_x",
            repository="o/r",
            title="T",
            text="short",
            state="active",
            created_by=None,
            created_by_display=None,
            created_at=0.0,
            updated_at=0.0,
            revision=1,
        )
        assert root_level(goal) == "epic"
        assert brief(goal, []) == {"title": "T", "goal": "short"}
        assert plan_depth([]) == 1


class Store:
    """The run store as the follow-up filer reads a run's assignment."""

    def __init__(self, raw: str | None) -> None:
        self.raw = raw

    def get_run_assignment(self, run_id: str) -> str | None:
        return self.raw


class TestLoopGuard:
    def test_follow_ups_carry_the_runs_chain_and_a_persons_run_carries_none(self) -> None:
        planned = AgentAssignment(
            lead="lantern", roles={}, agents={}, origin_agent="planner", chain_depth=2
        )
        person = AgentAssignment(lead="lantern", roles={}, agents={})
        assert origin_for_run(Store(None), "r1") is None  # type: ignore[arg-type]
        assert origin_for_run(Store(person.to_json()), "r1") is None  # type: ignore[arg-type]
        origin = origin_for_run(Store(planned.to_json()), "r1")  # type: ignore[arg-type]
        assert origin == WorkOrigin(agent_slug="planner", chain_depth=2)

        cand = Candidate("k" * 16, ReviewFollowup(title="T", body="B"), 1, "review")
        plain = issue_body(cand, run_id="r1", repo="o/r", pr_number=1, pr_url="", closes=None)
        assert origin_from_body(plain) is None
        marked = issue_body(
            cand, run_id="r1", repo="o/r", pr_number=1, pr_url="", closes=None, origin=origin
        )
        assert marked.startswith(plain)
        assert origin_from_body(marked) == origin
        # A marker the reviewer wrote is never read back as the daemon's.
        forged = Candidate(
            "k" * 16,
            ReviewFollowup(title="T", body="B " + origin_marker(WorkOrigin("x", None, 0))),
            1,
            "review",
        )
        body = issue_body(
            forged, run_id="r1", repo="o/r", pr_number=1, pr_url="", closes=None, origin=origin
        )
        assert origin_from_body(body) == origin
        assert body.count("lantern:origin") == 1

    def test_propose_run_follow_up_propose_stops_at_the_chain_ceiling(self, world: World) -> None:
        """Five generations end to end on the fake forge: each proposed plan
        is broken down, approved, published and run by the driver; each
        run files a follow-up as its assignment says; the next proposal
        reads the follow-ups. No plan is ever deeper than ``max_chain_depth``
        and no follow-up filed at that depth is ever read into a brief."""
        w = world
        ceiling = w.loop.config.agent_team.max_chain_depth
        assert ceiling == 2
        w.with_issue_source()
        w.grant("planner", "plan.propose")
        w.grants()
        goal = _goal(w)
        _followup(w, "Seed from a person's run")
        filed: dict[int, int] = {}  # follow-up number -> its depth
        depths: list[int] = []
        briefed: list[list[int]] = []
        for generation in range(5):
            w.tick()
            plan = next(p for p in _plans(w, goal) if not p.archived)
            row = next(r for r in _proposals(w, "allow") if r.plan_id == plan.id)
            depths.append(row.attrs["chain_depth"])
            briefed.append(list(row.attrs["followups"]))
            w.tick()
            w.deliver(plan)
            w.tick()
            w.later(DELAY)
            w.tick()
            w.tick()
            run = w.loop.epic_runs.runs.latest(plan.id, plan.root_id)
            assert run is not None
            (item,) = [w.h.dstore.get(t.item_id) for t in run.tasks if t.item_id]
            assigned = w.loop._assign(item, w.now())
            assignment = AgentAssignment.from_json(assigned.assignment_json)
            assert assignment.origin_agent == "planner"
            assert assignment.chain_depth == row.attrs["chain_depth"]

            origin = origin_for_run(Store(assigned.assignment_json), "r")  # type: ignore[arg-type]
            cand = Candidate(
                "a" * 16, ReviewFollowup(title=f"Gen {generation} note", body="x"), 1, "review"
            )
            body = issue_body(
                cand,
                run_id=f"g{generation}",
                repo="o/r",
                pr_number=1,
                pr_url="",
                closes=None,
                origin=origin,
            )
            ref = w.fake.issue_create("o/r", f"Gen {generation} note", body, labels=[LABEL])
            filed[ref.number] = origin.chain_depth if origin is not None else 0
            _archive(w, plan)
            w.later(EVERY)

        assert max(depths) <= ceiling
        assert depths == [1, 2, 2, 2, 2]
        too_deep = {n for n, d in filed.items() if d >= ceiling}
        assert too_deep, "the deepest generations did file follow-ups"
        assert all(not too_deep & set(b) for b in briefed), "never read into a brief"


def test_the_feature_is_served_with_planning() -> None:
    assert "goals.proposing" in features(Config())


def test_a_proposal_cut_short_is_settled_from_the_plan(world: World) -> None:
    w = world
    w.grant("planner", "plan.propose")
    goal = _goal(w)
    w.tick()
    (plan,) = _plans(w, goal)

    def claimed(target: str) -> str:
        spec = OperationSpec(
            action="plan.propose",
            target_kind="plan",
            target_key=target,
            principal=Principal.for_agent("planner"),
            request={"goal_id": goal.id},
        )
        op, _ = w.loop.operations.accept(spec, now=w.now())
        w.loop.operations.claim(op.id, "g_dead", w.now())
        return str(op.id)

    stored, lost = claimed(plan.id), claimed("plan_never_stored")
    reconcile_operations(w.loop, generation="g_new", now=w.now())
    assert w.loop.operations.get(stored).state == "succeeded"
    gone = w.loop.operations.get(lost)
    assert (gone.state, gone.error_code) == ("failed", "interrupted_before_effect")
