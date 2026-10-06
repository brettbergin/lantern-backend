"""A breakdown end to end: a plan item admitted for a node, dispatched by the
daemon to the REAL engine (echo backend, fake sbx), the planner's scripted
answer delivered to the plan record as ``proposed`` children — and nothing
written to the forge. The engine's own rules are in ``test_engine_plan.py``
and the route's in ``tests/api/test_plans.py``; this proves the wiring
between them: the item names its node, the daemon hands the run a desk over
the plan service, and the service writes the level under the node's rules
with the ``plan.generation.*`` events scoped to the run.
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from lantern import hostgit
from lantern.config import Config
from lantern.daemon.controls.intake import PlanAdmission, plan_item
from lantern.daemon.controls.results import ControlError
from lantern.daemon.loop import DaemonLoop
from lantern.daemon.sources import ApiSource, CompositeSource
from lantern.daemon.store import DaemonStore
from lantern.db.api_models import ApiEventRow
from lantern.engine.store import StateStore
from lantern.ghids import api_item_id
from lantern.plans.service import PlanService
from lantern.plans.store import PlanStore
from lantern.sbx.cli import SbxCLI
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.fakes.gitrepo import make_repo
from tests.unit.test_daemon_loop import FakeSource
from tests.unit.test_engine import Harness
from tests.unit.test_engine_plan import READY, asks, code_task, question, workload_task


def answer(*children: dict[str, Any]) -> dict[str, Any]:
    return {
        "json": {
            "root": {
                "title": "Export reports",
                "goal": "Download reports as CSV",
                "context": "The reports module handles exports",
                "acceptance_criteria": ["Reports export as CSV"],
            },
            "children": list(children),
        }
    }


REPO = "o/app"
PERSON = {"kind": "client", "id": "c1", "display": "Pat", "via": "api"}


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    origin = make_repo(tmp_path, "upstream", {"README.md": "# app\n"})
    monkeypatch.setattr(
        hostgit,
        "clone_from_remote",
        lambda url, target, branch, **kw: hostgit.clone_for_run(origin, target, branch),
    )
    return Harness(fake_sbx, tmp_path, monkeypatch)


class World:
    """The daemon, its stores and a plan with one epic to break down."""

    def __init__(self, harness: Harness, **config: Any) -> None:
        self.config = Config.model_validate(
            {
                "home": str(harness.home.root),
                "limits": {"disk_warn": 0, "disk_abort": 0, "mem_warn": 0},
                "github": {"repos": [{"repo": REPO}]},
                "workloads": [{"name": "research", "description": "reads the web"}],
            }
            | config
        )
        self.store = StateStore(harness.home.state_db)
        self.dstore = DaemonStore(harness.home.state_db)
        self.plans = PlanService(PlanStore(self.dstore), lambda: self.config)
        # The forge's source sees every GitHub call the daemon makes; the
        # plan item is an API item, so it is routed to the API source.
        self.github = FakeSource()
        self.harness = harness
        self.loop = self.new_loop()
        plan = self.plans.create(
            level="epic",
            repository=REPO,
            sections={"title": "Export reports", "goal": "download reports as CSV"},
            now=1.0,
            actor=PERSON,
        )
        self.plan_id, self.epic_id = plan.id, plan.root_id
        # One task a person drafted (it stays) and one the planner proposed
        # last time (the new proposal replaces it).
        plan, self.kept_id = self.plans.add_node(
            plan.id,
            expected_revision=plan.revision,
            parent_id=self.epic_id,
            repository=None,
            sections={"title": "Person's task", "kind": "code"},
            position=None,
            now=2.0,
            actor=PERSON,
        )
        plan, stale_id = self.plans.add_node(
            plan.id,
            expected_revision=plan.revision,
            parent_id=self.epic_id,
            repository=None,
            sections={"title": "Old proposal", "kind": "code"},
            position=None,
            now=3.0,
            actor=PERSON,
        )
        old = plan.node(stale_id)
        assert old is not None
        from dataclasses import replace

        plan = self.plans.store.apply(
            plan.id,
            expected_revision=plan.revision,
            now=4.0,
            upsert=[replace(old, state="proposed", origin="planner")],
        )
        self.revision = plan.revision

    def new_loop(self) -> DaemonLoop:
        return DaemonLoop(
            self.config,
            store=self.store,
            dstore=self.dstore,
            source=CompositeSource(self.github, None, None, ApiSource()),
            sbx=SbxCLI(binary=str(self.harness.fake_sbx.binary)),
            worker_python=sys.executable,
            install_workers=False,
        )

    def admit(self, note: str = "") -> str:
        item = plan_item(
            self.loop,
            PlanAdmission(self.plan_id, self.epic_id, expected_revision=self.revision, note=note),
            item_id=api_item_id("plan:k1"),
        )
        self.dstore.upsert_new(item, 5.0)
        return item.item_id

    def events(self, type_: str) -> list[ApiEventRow]:
        with self.dstore.read() as session:
            return list(
                session.scalars(
                    select(ApiEventRow).where(ApiEventRow.type == type_).order_by(ApiEventRow.seq)
                )
            )


def test_a_breakdown_runs_in_the_sandbox_and_lands_in_the_plan(harness: Harness) -> None:
    world = World(harness)
    item_id = world.admit(note="CSV only")
    harness.script(
        [READY, answer(code_task("c1"), code_task("c2", deps=["c1"]), workload_task("c3"))]
    )

    result = world.loop.tick()

    assert result.dispatched == item_id
    assert result.outcome == "done", result
    item = world.dstore.get(item_id)
    assert item is not None and item.state == "done" and item.run_id is not None
    run = world.store.get_run(item.run_id)
    assert run.kind == "plan" and run.state == "completed"
    assert run.outcome.startswith(
        "Generate the epic and its tasks from “Export reports”\n\nCSV only"
    )
    assert [p.sink for p in run.published] == ["plan"]
    # The level under the epic: the person's task stays, the old proposal
    # is gone, the planner's three are proposed with their links mapped.
    plan = world.plans.get(world.plan_id)
    assert plan.revision == world.revision + 1, "one write, one revision"
    assert plan.root.origin == "planner" and plan.root.state == "proposed"
    assert not plan.generation_pending and plan.input["title"] == "Export reports"
    assert plan.root.goal and plan.root.context and plan.root.acceptance_criteria
    assert plan.node(world.epic_id).generation is None, "a ready planner asked nothing"
    children = plan.children(world.epic_id)
    assert [c.title for c in children] == ["Person's task", "Task c1", "Task c2", "Survey c3"]
    kept, c1, c2, c3 = children
    assert (kept.id, kept.state, kept.origin) == (world.kept_id, "draft", "person")
    for child in (c1, c2, c3):
        assert child.state == "proposed" and child.origin == "planner"
        assert child.repository == REPO and child.level == "task"
        # The agent dispatch bound to the run's plan phase, by name.
        assert child.proposed_by == "agent:planner"
    assert plan.root.proposed_by == "agent:planner"
    assert kept.proposed_by == PERSON["id"], "the person's task is still theirs"
    assert c1.kind == "code" and c1.verify_commands == ("make test",)
    assert c1.acceptance_criteria == ("c1 works",)
    assert c2.depends_on == (c1.id,)
    assert c3.kind == "workload" and c3.workload_profile == "research"
    assert [c.position for c in children] == [0, 1, 2, 3]
    # The generation's events, scoped to the run and its item.
    (started,) = world.events("plan.generation.started")
    (proposed,) = world.events("plan.generation.proposed")
    assert world.events("plan.generation.failed") == []
    public = f"run_{item.run_id}"
    assert json.loads(started.data_json or "{}") == {
        "plan_id": world.plan_id,
        "node_id": world.epic_id,
        "run_id": public,
    }
    assert json.loads(proposed.data_json or "{}") == {
        "plan_id": world.plan_id,
        "node_id": world.epic_id,
        "run_id": public,
        "kind": "breakdown",
        "count": 3,
    }
    assert proposed.run_id == item.run_id and proposed.item_id == item_id
    # Nothing reached the forge: its source was never asked a thing.
    assert world.github.calls == []
    assert harness.sandboxes_left() == []


def test_a_proposal_invalid_twice_is_a_failed_generation(harness: Harness) -> None:
    world = World(harness, daemon={"max_attempts_per_item": 1})
    item_id = world.admit()
    bad = answer(code_task("c1", acceptance_criteria=[]))
    harness.script([READY, bad, bad])

    result = world.loop.tick()

    assert result.outcome == "failed", result
    item = world.dstore.get(item_id)
    assert item is not None and item.run_id is not None
    (failed,) = world.events("plan.generation.failed")
    data = json.loads(failed.data_json or "{}")
    assert data["run_id"] == f"run_{item.run_id}"
    assert data["reason"].startswith("the planner's proposal was invalid twice:")
    assert world.events("plan.generation.proposed") == []
    plan = world.plans.get(world.plan_id)
    assert plan.revision == world.revision, "a failed generation writes nothing"
    assert "Old proposal" in [c.title for c in plan.children(world.epic_id)]


def test_a_node_already_being_broken_down_is_not_queued_twice(harness: Harness) -> None:
    world = World(harness)
    world.admit()
    with pytest.raises(ControlError) as refused:
        plan_item(
            world.loop,
            PlanAdmission(world.plan_id, world.epic_id, expected_revision=world.revision),
            item_id=api_item_id("plan:k2"),
        )
    assert refused.value.code == "already_in_progress"
    assert refused.value.detail["plan_code"] == "generation_in_progress"


# -- a plan that advances itself: reviewed, never asked ---------------------------


def _advance_auto(world: World) -> None:
    plan = world.plans.update(
        world.plan_id,
        expected_revision=world.revision,
        sections={},
        now=4.5,
        actor=PERSON,
        advance="auto",
    )
    world.revision = plan.revision


def verdict(decision: str, *reasons: str) -> dict[str, Any]:
    return {"json": {"verdict": decision, "reasons": list(reasons)}}


@pytest.mark.parametrize(
    ("decision", "reasons"),
    [("approve", ()), ("escalate", ("Task c2 overlaps the person's task.", "c1 is too big."))],
)
def test_an_auto_plans_breakdown_is_reviewed_and_the_verdict_lands_with_it(
    harness: Harness, decision: str, reasons: tuple[str, ...]
) -> None:
    from lantern.plans.model import review_digest, review_is_current

    # The repository allows questions; a plan that advances itself asks none.
    world = World(harness, planning={"max_questions": 3}, keep_sandboxes=True)
    _advance_auto(world)
    item_id = world.admit()
    harness.script(
        [answer(code_task("c1"), code_task("c2", deps=["c1"])), verdict(decision, *reasons)]
    )

    result = world.loop.tick()

    assert result.outcome == "done", result
    assert harness.consumed() == 2, "no clarifying turn: the proposal, then the review"
    item = world.dstore.get(item_id)
    assert item is not None and item.run_id is not None
    run_id = item.run_id
    assert world.events("plan.generation.questions") == []
    plan = world.plans.get(world.plan_id)
    assert plan.revision == world.revision + 1, "the proposal and its review: one write"
    node = plan.node(world.epic_id)
    assert node is not None and node.review is not None
    review = node.review
    assert review.verdict == decision and review.reasons == reasons
    assert review.run_id == run_id
    # The run's critic, as dispatch bound it.
    assert review.reviewed_by == "agent:critic"
    assert review.at > 0
    # The root was generated with the level, so the review covers it too,
    # and it is current on the record as written.
    assert review.digest == review_digest(plan, node, include_node=True)
    assert review_is_current(plan, node)
    assert [c.title for c in plan.children(world.epic_id)] == [
        "Person's task",
        "Task c1",
        "Task c2",
    ]
    (reviewed,) = world.events("plan.generation.reviewed")
    assert json.loads(reviewed.data_json or "{}") == {
        "plan_id": world.plan_id,
        "node_id": world.epic_id,
        "run_id": f"run_{run_id}",
        "verdict": decision,
        "reason_count": len(reasons),
    }
    assert reviewed.run_id == run_id and reviewed.item_id == item_id
    (proposed,) = world.events("plan.generation.proposed")
    assert proposed.seq < reviewed.seq
    # The review turn ran as the critic, recorded and charged as a phase.
    rows = [(r.phase, r.status, r.agent_slug) for r in world.store.phase_attempts(run_id)]
    assert rows == [("propose", "ok", "planner"), ("plan_review", "ok", "critic")]
    # Nothing reached the forge.
    assert world.github.calls == []


def test_a_manual_plans_breakdown_is_not_reviewed(harness: Harness) -> None:
    world = World(harness)
    world.admit()
    harness.script([READY, answer(code_task("c1"))])
    assert world.loop.tick().outcome == "done"
    assert harness.consumed() == 2, "the clarifying turn and the proposal, no review"
    plan = world.plans.get(world.plan_id)
    assert plan.node(world.epic_id).review is None
    assert world.events("plan.generation.reviewed") == []


def test_an_unusable_review_escalates_on_the_record(harness: Harness) -> None:
    world = World(harness)
    _advance_auto(world)
    world.admit()
    harness.script([answer(code_task("c1")), verdict("yes"), verdict("approve", "x" * 600)])
    assert world.loop.tick().outcome == "done"
    review = world.plans.get(world.plan_id).node(world.epic_id).review
    assert review is not None
    assert review.verdict == "escalate"
    assert review.reasons == ("the reviewer did not return a usable verdict",)


# -- clarifying questions (#2345) -------------------------------------------------

FORMATS = question("fmt", "Which formats?", "csv", "pdf")
READERS = question("who", "Who downloads them?", "staff", "public")


def _park(world: World, *questions: dict[str, Any]) -> str:
    """Admit a breakdown whose planner asks ``questions``; tick it to its
    park. The item id."""
    item_id = world.admit()
    world.harness.script([asks(*(questions or (FORMATS,)))])
    result = world.loop.tick()
    assert result.outcome == "awaiting_answers", result
    return item_id


def test_questions_park_the_run_until_they_are_answered(harness: Harness) -> None:
    from lantern.engine.planning import PlanAnswer

    world = World(harness, keep_sandboxes=True)
    item_id = _park(world, FORMATS, READERS)

    # Parked: the item waits, the run holds nothing, the plan holds the
    # questions, and the questions went out as an event scoped to the run.
    item = world.dstore.get(item_id)
    assert item is not None and item.state == "awaiting_answers" and item.run_id is not None
    run_id = item.run_id
    assert world.store.get_run(run_id).state == "awaiting_answers"
    plan = world.plans.get(world.plan_id)
    waiting = plan.node(world.epic_id).generation
    assert waiting is not None and waiting.status == "awaiting_answers"
    assert waiting.run_id == run_id and [q.id for q in waiting.questions] == ["fmt", "who"]
    assert plan.revision == world.revision + 1
    (asked,) = world.events("plan.generation.questions")
    data = json.loads(asked.data_json or "{}")
    assert data["run_id"] == f"run_{run_id}" and data["node_id"] == world.epic_id
    assert [q["id"] for q in data["questions"]] == ["fmt", "who"]
    assert data["questions"][0]["choices"][0] == {
        "value": "csv",
        "label": "CSV",
        "description": "about csv",
    }
    assert asked.run_id == run_id and asked.item_id == item_id
    assert world.events("plan.generation.failed") == []
    # A second breakdown of the node waits for this one.
    with pytest.raises(ControlError) as refused:
        plan_item(
            world.loop,
            PlanAdmission(world.plan_id, world.epic_id, expected_revision=plan.revision),
            item_id=api_item_id("plan:k2"),
        )
    assert refused.value.code == "already_in_progress"
    # Nothing to do while it waits: dispatch does not see it.
    assert world.loop.tick().dispatched is None

    outcome = world.loop.answer_plan_questions(
        world.plan_id,
        world.epic_id,
        answers={"fmt": PlanAnswer(value="pdf"), "who": PlanAnswer(text="the finance team")},
        skip=False,
        actor=PERSON,
    )
    assert outcome.resumed and outcome.item_id == item_id
    assert outcome.clarification.status == "answered"
    assert outcome.clarification.answered_by == "Pat"
    item = world.dstore.get(item_id)
    assert item is not None and item.state == "queued" and item.run_id == run_id
    (answered,) = world.events("plan.generation.answered")
    assert json.loads(answered.data_json or "{}")["answers"] == {
        "fmt": {"value": "pdf"},
        "who": {"text": "the finance team"},
    }

    harness.script([answer(code_task("c1"))])
    result = world.loop.tick()
    assert result.outcome == "done", result
    assert world.store.get_run(run_id).state == "completed"
    assert harness.consumed() == 1, "the questions are not asked again"
    prompts = [j["prompt"] for j in harness.agent_jobs(run_id) if j["kind"] == "agent.session"]
    (propose,) = prompts
    assert "Answer: PDF (`pdf`)" in propose
    assert "Answer: in their words: the finance team" in propose
    children = world.plans.get(world.plan_id).children(world.epic_id)
    assert [c.title for c in children] == ["Person's task", "Task c1"]


def test_a_skip_proposes_with_no_answers(harness: Harness) -> None:
    world = World(harness, keep_sandboxes=True)
    item_id = _park(world)
    outcome = world.loop.answer_plan_questions(
        world.plan_id, world.epic_id, answers={}, skip=True, actor=PERSON
    )
    assert outcome.resumed and outcome.clarification.status == "skipped"
    harness.script([answer(code_task("c1"))])
    assert world.loop.tick().outcome == "done"
    item = world.dstore.get(item_id)
    assert item is not None and item.run_id is not None
    (propose,) = [
        j["prompt"] for j in harness.agent_jobs(item.run_id) if j["kind"] == "agent.session"
    ]
    assert "The person skipped these questions" in propose


def test_no_questions_allowed_means_no_clarifying_turn(harness: Harness) -> None:
    world = World(harness, planning={"max_questions": 0})
    item_id = world.admit()
    harness.script([answer(code_task("c1"))])
    assert world.loop.tick().outcome == "done"
    assert harness.consumed() == 1
    item = world.dstore.get(item_id)
    assert item is not None and item.run_id is not None
    assert world.events("plan.generation.questions") == []


def test_the_repositorys_question_cap_is_the_planners(harness: Harness) -> None:
    world = World(
        harness,
        planning={"max_questions": 4},
        github={"repos": [{"repo": REPO, "planning": {"max_questions": 1}}]},
        keep_sandboxes=True,
    )
    world.admit()
    harness.script([READY, answer(code_task("c1"))])
    assert world.loop.tick().outcome == "done"
    item = next(i for i in world.dstore.items() if i.kind == "plan")
    assert item.run_id is not None
    prompts = [j["prompt"] for j in harness.agent_jobs(item.run_id) if j["kind"] == "agent.session"]
    clarify = next(p for p in prompts if p.startswith("# Before you propose"))
    assert "**at most 1** questions" in " ".join(clarify.split())


def test_a_restart_while_waiting_keeps_the_wait_and_takes_the_answer(harness: Harness) -> None:
    from lantern.engine.planning import PlanAnswer

    world = World(harness)
    item_id = _park(world)
    item = world.dstore.get(item_id)
    assert item is not None and item.run_id is not None
    run_id = item.run_id

    # The daemon goes away and comes back: recovery leaves the wait alone.
    world.loop = world.new_loop()
    world.loop.recover()
    item = world.dstore.get(item_id)
    assert item is not None and item.state == "awaiting_answers" and item.run_id == run_id
    assert world.loop.tick().dispatched is None

    world.loop.answer_plan_questions(
        world.plan_id,
        world.epic_id,
        answers={"fmt": PlanAnswer(value="csv")},
        skip=False,
        actor=PERSON,
    )
    harness.script([answer(code_task("c1"))])
    assert world.loop.tick().outcome == "done"
    assert world.store.get_run(run_id).state == "completed"


def test_a_park_the_daemon_died_before_settling_is_settled_at_recovery(harness: Harness) -> None:
    world = World(harness)
    item_id = _park(world)
    # As if the process died between the engine's park and the settle.
    world.dstore._update(item_id, 9.0, state="running")
    world.loop = world.new_loop()
    world.loop.recover()
    item = world.dstore.get(item_id)
    assert item is not None and item.state == "awaiting_answers"


def test_abandoning_a_waiting_run_withdraws_its_questions(harness: Harness) -> None:
    from lantern.engine.planning import PlanAnswer
    from lantern.plans.service import PlanRefusal

    world = World(harness)
    item_id = _park(world)
    world.loop.abandon_item(item_id, "not now")
    waiting = world.plans.get(world.plan_id).node(world.epic_id).generation
    assert waiting is not None and waiting.status == "withdrawn"
    (failed,) = world.events("plan.generation.failed")
    assert "not now" in json.loads(failed.data_json or "{}")["reason"]
    with pytest.raises(PlanRefusal) as refused:
        world.loop.answer_plan_questions(
            world.plan_id,
            world.epic_id,
            answers={"fmt": PlanAnswer(value="csv")},
            skip=False,
            actor=PERSON,
        )
    assert refused.value.code == "already_answered"


def test_answers_are_held_to_the_questions(harness: Harness) -> None:
    from lantern.engine.planning import PlanAnswer
    from lantern.plans.service import PlanRefusal

    world = World(harness)
    _park(world, FORMATS, question("strict", "Pick one", "a", "b", free=False))
    for answers, skip, detail in (
        ({"nope": PlanAnswer(value="csv")}, False, "no question 'nope'"),
        ({"fmt": PlanAnswer(value="xml")}, False, "'xml' is not a choice"),
        ({"strict": PlanAnswer(text="neither")}, False, "takes one of its choices"),
        ({"fmt": PlanAnswer()}, False, "names no choice and has no text"),
        ({"fmt": PlanAnswer(value="csv")}, True, "not both"),
        ({}, False, "answer at least one question"),
    ):
        with pytest.raises(PlanRefusal) as refused:
            world.loop.answer_plan_questions(
                world.plan_id, world.epic_id, answers=answers, skip=skip, actor=PERSON
            )
        assert refused.value.status == 422 and detail in refused.value.detail, detail
    # One answer from chat is recorded and the run keeps waiting for the
    # other; the second settles it.
    first = world.loop.answer_plan_questions(
        world.plan_id,
        world.epic_id,
        answers={"fmt": PlanAnswer(value="csv")},
        skip=False,
        actor=PERSON,
        settle=False,
    )
    assert not first.resumed and first.clarification.status == "awaiting_answers"
    second = world.loop.answer_plan_questions(
        world.plan_id,
        world.epic_id,
        answers={"strict": PlanAnswer(value="b")},
        skip=False,
        actor=PERSON,
        settle=False,
    )
    assert second.resumed and second.clarification.status == "answered"
    assert set(second.clarification.answers) == {"fmt", "strict"}


# -- a re-plan (#2346) ------------------------------------------------------------


class _Provisioner:
    def clone_token(self, repo: str) -> None:
        return None

    def gh_bot_login(self, repo: str) -> None:
        return None


class ForgeBox:
    """The daemon's forge connection, answered by a fake."""

    kind = "github"
    provisioned = True

    def __init__(self, ops: FakeGithub) -> None:
        self.ops_obj = ops
        self.provisioner = _Provisioner()

    def ops(self) -> FakeGithub:
        return self.ops_obj

    def call(self, fn: Any) -> Any:
        return fn(self.ops_obj)

    def note_failure(self, exc: BaseException) -> bool:
        return False


def _published_epic(world: World, fake: FakeGithub) -> tuple[str, str, int]:
    """A second plan: an epic with tasks A and B, published to ``fake``;
    its id, the epic's and its revision."""
    plan = world.plans.create(
        level="epic",
        repository=REPO,
        sections={"title": "Import reports"},
        now=10.0,
        actor=PERSON,
    )
    for title in ("A", "B"):
        plan, _ = world.plans.add_node(
            plan.id,
            expected_revision=plan.revision,
            parent_id=plan.root_id,
            repository=None,
            sections={
                "title": title,
                "kind": "code",
                "acceptance_criteria": [f"{title} works"],
                "verify_commands": ["make test"],
            },
            position=None,
            now=11.0,
            actor=PERSON,
        )
    plan = world.plans.store.apply(
        plan.id,
        expected_revision=plan.revision,
        now=11.5,
        upsert=[replace(plan.root, **plan.input, origin="planner")],
    )
    plan = world.plans.approve(
        plan.id,
        plan.root_id,
        expected_revision=plan.revision,
        node_ids=None,
        now=12.0,
        actor=PERSON,
    )
    level = world.plans.publish(
        plan.id,
        plan.root_id,
        expected_revision=plan.revision,
        forge_kind="github",
        connect=lambda: fake,
        clock=lambda: 13.0,
        actor=PERSON,
    )
    assert [r.outcome for r in level.results] == ["created", "created", "created"]
    return plan.id, plan.root_id, level.plan.revision


def test_a_replan_reads_the_forge_first_and_leaves_a_diff_on_the_plan(harness: Harness) -> None:
    world = World(harness)
    fake = FakeGithub()
    plan_id, epic_id, revision = _published_epic(world, fake)
    world.loop.github = ForgeBox(fake)
    plan = world.plans.get(plan_id)
    a, b = plan.children(epic_id)
    assert a.forge is not None
    # A person renames A on the forge before the re-plan runs.
    fake.person_edits(REPO, a.forge.number, title="A, renamed")
    item = plan_item(
        world.loop,
        PlanAdmission(plan_id, epic_id, expected_revision=revision, note="B is done elsewhere"),
        item_id=api_item_id("plan:replan"),
    )
    assert item.title == "Re-plan the tasks of “Import reports”"
    world.dstore.upsert_new(item, 14.0)
    writes = len([c for c in fake.raw_calls if c[0] != "GET"])
    created = len(fake.issues_created)
    harness.script(
        [
            READY,
            {
                "json": {
                    "add": [code_task("c1") | {"rationale": "a step is missing"}],
                    "modify": [{"target": a.id, "goal": "A, sharper", "rationale": "r"}],
                    "suggest_close": [{"target": b.id, "rationale": "done elsewhere"}],
                }
            },
        ]
    )

    result = world.loop.tick()

    assert result.dispatched == item.item_id
    assert result.outcome == "done", result
    run_item = world.dstore.get(item.item_id)
    assert run_item is not None and run_item.run_id is not None
    plan = world.plans.get(plan_id)
    # The forge was read before the planner was asked: A's new title is in.
    assert plan.node(a.id) is not None and plan.node(a.id).title == "A, renamed"  # type: ignore[union-attr]
    epic = plan.node(epic_id)
    assert epic is not None and epic.replan is not None
    assert [e.action for e in epic.replan.entries] == ["add", "modify", "suggest_close"]
    assert epic.replan.run_id == f"run_{run_item.run_id}"
    assert [c.title for c in plan.children(epic_id)] == ["A, renamed", "B"], "nothing added yet"
    (proposed,) = world.events("plan.generation.proposed")
    data = json.loads(proposed.data_json or "{}")
    assert (data["kind"], data["add"], data["modify"], data["suggest_close"]) == (
        "replan",
        1,
        1,
        1,
    )
    assert proposed.run_id == run_item.run_id
    # Nothing was written to the forge: the run only read it.
    assert len([c for c in fake.raw_calls if c[0] != "GET"]) == writes
    assert len(fake.issues_created) == created and fake.issues_closed == []


def test_a_replan_that_cannot_read_the_forge_fails_named(harness: Harness) -> None:
    world = World(harness, daemon={"max_attempts_per_item": 1})
    fake = FakeGithub()
    plan_id, epic_id, revision = _published_epic(world, fake)
    world.loop.github = None
    item = plan_item(
        world.loop,
        PlanAdmission(plan_id, epic_id, expected_revision=revision),
        item_id=api_item_id("plan:replan"),
    )
    world.dstore.upsert_new(item, 14.0)
    harness.script([READY])

    result = world.loop.tick()

    assert result.outcome == "failed", result
    (failed,) = world.events("plan.generation.failed")
    reason = json.loads(failed.data_json or "{}")["reason"]
    assert reason.startswith("the forge could not be read before re-planning:")
    assert "no forge connection" in reason
    assert world.plans.get(plan_id).node(epic_id).replan is None  # type: ignore[union-attr]
