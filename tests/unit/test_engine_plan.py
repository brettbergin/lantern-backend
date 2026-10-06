"""A ``plan`` run: one level of a plan proposed from a read-only checkout.

The planner reads a checkout the host cut into the data directory — never
with a credential in the sandbox, never through a github box — answers in
JSON held to the level's rules and cap with one retry, and the answer goes
to the plan record through the engine's :class:`PlanDesk`. These tests use
a recording desk; the daemon's (the plan service) is covered with the
daemon in ``test_plan_generation.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from lantern import hostgit
from lantern.engine.model import PLAN_STAGES, RESUMABLE_RUN_STATES, TERMINAL_RUN_STATES
from lantern.engine.planning import (
    Clarification,
    CurrentChild,
    PlanAnswer,
    PlanBrief,
    PlanClarification,
    PlanDelivery,
    PlanProposal,
    PlanQuestion,
    PlanReplan,
    PlanVerdict,
    ProfileRef,
    proposal_problems,
    replan_problems,
)
from lantern.errors import ConfigError, PlanDeliveryError, WorkerError
from lantern.events import HostEventTypes
from lantern.sbx.naming import run_name
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.fakes.gitrepo import commit_files, make_repo, open_repo
from tests.unit.test_engine import Harness

PLAN_STATES = ["provisioning", "proposing", "completed"]
REPO = "o/app"


def epic_brief(**over: Any) -> PlanBrief:
    fields: dict[str, Any] = {
        "plan_id": "plan_1",
        "node_id": "node_epic",
        "level": "epic",
        "child_level": "task",
        "repository": REPO,
        "title": "Export reports",
        "goal": "People can download their reports as CSV",
        "acceptance_criteria": ["a report downloads as CSV"],
        "room": 3,
        "cap": 3,
        "profiles": [ProfileRef(name="research", description="reads the web")],
    }
    return PlanBrief.model_validate(fields | over)


def code_task(id: str, *, deps: list[str | int] | None = None, **over: Any) -> dict[str, Any]:
    child: dict[str, Any] = {
        "id": id,
        "title": f"Task {id}",
        "goal": f"goal of {id}",
        "context": "src/reports.py builds the report",
        "acceptance_criteria": [f"{id} works"],
        "kind": "code",
        "verify_commands": ["make test"],
        "depends_on": deps or [],
    }
    return child | over


def workload_task(id: str, profile: str = "research") -> dict[str, Any]:
    return {
        "id": id,
        "title": f"Survey {id}",
        "goal": f"what {id} finds out",
        "context": "docs/formats.md lists the formats people asked for",
        "acceptance_criteria": ["a summary lists three formats"],
        "kind": "workload",
        "workload_profile": profile,
    }


def answer(*children: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"children": list(children)}}


READY: dict[str, Any] = {"json": {"ready": True}}


def question(id: str, prompt: str, *values: str, free: bool = True) -> dict[str, Any]:
    return {
        "id": id,
        "prompt": prompt,
        "choices": [{"value": v, "label": v.upper(), "description": f"about {v}"} for v in values],
        "allow_free_text": free,
    }


def asks(*questions: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"questions": list(questions)}}


@dataclass
class RecordingDesk:
    """A plan record that remembers what it was told."""

    plan_brief: PlanBrief = field(default_factory=epic_brief)
    started_runs: list[str] = field(default_factory=list)
    delivered: list[tuple[str, PlanProposal]] = field(default_factory=list)
    replans: list[tuple[str, PlanReplan]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    refuse: str | None = None
    #: The node's clarification as the record holds it, and every ask.
    clarification: Clarification | None = None
    asked: list[tuple[str, list[PlanQuestion]]] = field(default_factory=list)
    #: Each brief asked for, and whether it was the fresh one the planner
    #: is about to be given.
    briefs: list[bool] = field(default_factory=list)
    #: The verdict delivered with each proposal (None: not reviewed).
    reviews: list[PlanVerdict | None] = field(default_factory=list)

    def brief(self, *, fresh: bool = False) -> PlanBrief:
        self.briefs.append(fresh)
        return self.plan_brief.model_copy(update={"clarification": self.clarification})

    def started(self, run_id: str) -> None:
        self.started_runs.append(run_id)

    def ask(self, run_id: str, questions: Any) -> None:
        if self.refuse is not None:
            raise PlanDeliveryError(self.refuse)
        self.asked.append((run_id, list(questions)))
        self.clarification = Clarification(run_id=run_id, questions=list(questions))

    def answer(self, skip: bool = False, **answers: PlanAnswer) -> None:
        """A person settles the questions on the record."""
        assert self.clarification is not None
        self.clarification = self.clarification.model_copy(
            update={"answers": answers, "status": "skipped" if skip else "answered"}
        )

    def deliver(
        self, run_id: str, proposal: PlanProposal, *, review: PlanVerdict | None = None
    ) -> PlanDelivery:
        if self.refuse is not None:
            raise PlanDeliveryError(self.refuse)
        self.delivered.append((run_id, proposal))
        self.reviews.append(review)
        return PlanDelivery(len(proposal.children), f"plan plan_1/{self.plan_brief.node_id}")

    def deliver_replan(self, run_id: str, replan: PlanReplan) -> PlanDelivery:
        if self.refuse is not None:
            raise PlanDeliveryError(self.refuse)
        self.replans.append((run_id, replan))
        return PlanDelivery(replan.count, f"plan plan_1/{self.plan_brief.node_id} (re-plan)")

    def failed(self, run_id: str, reason: str) -> None:
        self.failures.append((run_id, reason))


@pytest.fixture
def harness(fake_sbx: FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(fake_sbx, tmp_path, monkeypatch)


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """The repository's remote, cloned on the host in place of a fetch;
    the (url, token) pairs each clone was made with."""
    origin = make_repo(
        tmp_path,
        "upstream",
        {"README.md": "# app\n", "AGENTS.md": "Run make test before a PR.\n"},
    )
    seen: list[tuple[str, str]] = []

    def fake_clone(url: str, target: Path, branch: str, **kwargs: object) -> str:
        seen.append((url, str(kwargs.get("token"))))
        return hostgit.clone_for_run(origin, target, branch)

    monkeypatch.setattr(hostgit, "clone_from_remote", fake_clone)
    return seen


def engine(harness: Harness, desk: RecordingDesk, **over: Any) -> Any:
    built = harness.engine(github={"repos": [{"repo": REPO}]}, **over)
    built.plan_desk = desk
    return built


class TestPlanRun:
    def test_a_level_is_proposed_from_the_checkout_and_delivered_to_the_plan(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk()
        harness.script([answer(code_task("c1"), code_task("c2", deps=["c1"]), workload_task("c3"))])
        calls: list[str] = []
        fake = FakeGithub()

        def ops(client: object, run_id: str) -> FakeGithub:
            calls.append(run_id)
            return fake

        built = engine(harness, desk, keep_sandboxes=True)
        built._github_ops = ops
        result = built.start("Propose the tasks of “Export reports”", repo=REPO, kind="plan")

        assert result.state == "completed", result.reason
        assert result.kind == "plan"
        assert harness.run_states() == PLAN_STATES
        assert harness.consumed() == 1
        # The record heard the start and got the validated answer.
        assert desk.started_runs == [result.run_id]
        ((run_id, proposal),) = desk.delivered
        assert run_id == result.run_id
        assert [c.title for c in proposal.children] == ["Task c1", "Task c2", "Survey c3"]
        assert proposal.dependencies() == [[], [0], []]
        assert desk.failures == []
        # The checkout was cut on the host with the host's own credential,
        # into the data directory the sandbox reads.
        assert upstream == [("https://github.com/o/app", "gh_tok")]
        checkout = harness.home.runs / result.run_id / "workspace" / "app"
        assert (checkout / "README.md").is_file()
        # One agent box, no github box, and nothing written to the forge.
        assert harness.sandboxes_left() == [run_name(harness.home, result.run_id, "agent")]
        assert calls == [], "a plan run never builds forge ops"
        assert fake.issues_created == [] and fake.issue_comments_posted == []
        assert fake.labels_created == [] and fake.merges == [] and fake.raw_calls == []
        # The planner ran read-only, as the planner, on the plan model key.
        sessions = [j for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"]
        (job,) = sessions
        assert job["permission_mode"] == "read_only"
        assert "Propose the tasks of one epic" in job["prompt"]
        assert "Run make test before a PR." in job["prompt"], "the repository's own conventions"
        assert "`research` — reads the web" in job["prompt"]
        # The run's record: the task's output, the phase row, where it went.
        (task,) = result.tasks
        assert task.state == "done" and task.output is not None
        assert task.output.summary.startswith("Proposed 3 tasks for the epic “Export reports”")
        assert result.summary == task.output.summary
        ((entry),) = result.published
        assert entry.sink == "plan" and entry.location == "plan plan_1/node_epic"
        rows = [(r.phase, r.status) for r in built.store.phase_attempts(result.run_id)]
        assert rows == [("propose", "ok")]

    def test_the_stages_are_resumable_and_named(self) -> None:
        assert PLAN_STAGES == ("clarifying", "awaiting_answers", "proposing")
        assert set(PLAN_STAGES) <= RESUMABLE_RUN_STATES
        # The park is terminal for liveness, like a held workload.
        assert "awaiting_answers" in TERMINAL_RUN_STATES

    def test_an_invalid_answer_is_sent_back_once_with_the_problems(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk()
        harness.script(
            [
                answer(code_task("c1", acceptance_criteria=[]), code_task("c2", deps=["c9"])),
                answer(code_task("c1"), code_task("c2", deps=["c1"])),
            ]
        )
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.consumed() == 2
        second = [j for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"]
        retried = max(second, key=lambda j: len(j["prompt"]))["prompt"]
        assert "Previous attempt was invalid" in retried
        assert "every child needs at least one acceptance criterion" in retried
        assert "depends on 'c9', which is not a sibling" in retried
        assert len(desk.delivered) == 1

    def test_invalid_twice_fails_the_run_and_tells_the_plan(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk()
        bad = answer(code_task("c1", verify_commands=[]))
        harness.script([bad, bad])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "failed"
        assert result.reason is not None
        assert result.reason.startswith("the planner's proposal was invalid twice:")
        assert "a code task needs at least one verify command" in result.reason
        assert desk.delivered == []
        assert desk.failures == [(result.run_id, result.reason)]
        (task,) = result.tasks
        assert task.state == "failed"
        assert harness.run_states() == ["provisioning", "proposing", "failed"]

    def test_the_level_cap_is_enforced(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(room=2, cap=4, kept=["Kept task", "Other"]))
        too_many = answer(code_task("c1"), code_task("c2"), code_task("c3"))
        harness.script([too_many, too_many])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "failed"
        assert "propose at most 2 tasks; this answer has 3" in (result.reason or "")

    def test_a_workload_task_names_a_configured_profile(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk()
        harness.script(
            [answer(workload_task("c1", profile="nowhere")), answer(workload_task("c1"))]
        )
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        prompts = [j["prompt"] for j in harness.agent_jobs(result.run_id) if "prompt" in j]
        assert any(
            "workload profile `nowhere` is not configured (configured: research)" in p
            for p in prompts
        )
        ((_, proposal),) = desk.delivered
        assert proposal.children[0].workload_profile == "research"

    def test_a_refused_delivery_fails_the_run_named(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(refuse="the plan was deleted")
        harness.script([answer(code_task("c1"))])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "failed"
        assert result.reason == "the plan would not take the proposal: the plan was deleted"
        assert desk.failures == [(result.run_id, result.reason)]

    def test_a_resume_after_the_turn_delivers_without_asking_again(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(refuse="the plan is busy")
        harness.script([answer(code_task("c1"))])
        first = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert first.state == "failed"
        desk.refuse = None
        harness.script([])
        harness.events.clear()
        resumed = engine(harness, desk).resume(first.run_id)
        assert resumed.state == "completed", resumed.reason
        assert harness.consumed() == 0, "the persisted proposal is delivered as it stands"
        assert len(desk.delivered) == 1

    def test_a_plan_run_needs_a_record(self, harness: Harness) -> None:
        with pytest.raises(ConfigError, match="plan record"):
            harness.engine().start("plan", kind="plan")

    def test_chat_before_the_turn_steers_the_proposal(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk()
        steer = {
            "json": {
                "reply": "noted — CSV only",
                "action": "steer_run",
                "guidance": "keep it to CSV; no PDF export",
            }
        }
        harness.script([steer, answer(code_task("c1"))])
        built = engine(harness, desk, keep_sandboxes=True)
        built.post_user_message("only CSV please")
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        prompts = [j["prompt"] for j in harness.agent_jobs(result.run_id) if "prompt" in j]
        # The steering prompt quotes the run's goal too; the job files come
        # back in no particular order.
        planner = next(p for p in prompts if "Propose the tasks" in p and "steering stage" not in p)
        assert "keep it to CSV; no PDF export" in planner
        assert any(e.type == HostEventTypes.CHAT_REPLY for e in harness.events)


def sessions(harness: Harness, run_id: str) -> list[str]:
    """Every agent session's prompt, oldest first (keep_sandboxes runs)."""
    jobs = [j for j in harness.agent_jobs(run_id) if j["kind"] == "agent.session"]
    return [j["prompt"] for j in sorted(jobs, key=lambda j: j.get("created_at", 0))]


class TestClarify:
    """The clarifying turn in front of the proposal (#2345)."""

    @pytest.mark.parametrize("generate_root", [False, True])
    def test_ready_goes_straight_to_proposing(
        self, harness: Harness, upstream: list[tuple[str, str]], generate_root: bool
    ) -> None:
        brief = epic_brief(
            max_questions=3,
            generate_root=generate_root,
            input={"title": "Make reports portable"} if generate_root else {},
        )
        desk = RecordingDesk(plan_brief=brief)
        response = answer(code_task("c1"))
        if generate_root:
            response["json"]["root"] = {
                "title": "Export reports",
                "goal": "Download reports as CSV",
                "context": "The reports module provides the export path",
                "acceptance_criteria": ["The downloaded CSV preserves the displayed rows"],
            }
        harness.script([READY, response])
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.run_states() == ["provisioning", "clarifying", "proposing", "completed"]
        assert harness.consumed() == 2
        assert desk.asked == [] and len(desk.delivered) == 1
        rows = [(r.phase, r.status) for r in built.store.phase_attempts(result.run_id)]
        assert rows == [("clarify", "ok"), ("propose", "ok")]
        prompts = [
            j["prompt"] for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"
        ]
        clarify = next(p for p in prompts if p.startswith("# Before you propose"))
        assert "**at most 3** questions" in " ".join(clarify.split())
        assert "Run make test before a PR." in clarify, "the repository's own conventions"
        propose = next(p for p in prompts if p.startswith("# Propose"))
        assert "(no questions were asked)" in propose
        if generate_root:
            assert "Make reports portable" in clarify and "Make reports portable" in propose
            assert "Return a root object" not in clarify
            assert "include a `root` object" in propose
            assert desk.delivered[0][1].source_input == brief.input
        # One checkout serves both turns.
        assert len(upstream) == 1

    def test_questions_park_the_run_and_the_answers_reach_the_proposal(
        self, harness: Harness, upstream: list[tuple[str, str]], tmp_path: Path
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=3))
        harness.script(
            [
                asks(
                    question("fmt", "Which formats?", "csv", "pdf"),
                    question("who", "Who downloads them?", "staff", "public"),
                )
            ]
        )
        parked = engine(harness, desk).start("plan", repo=REPO, kind="plan")

        # Parked, holding nothing: no sandbox kept, no failure told.
        assert parked.state == "awaiting_answers", parked.reason
        assert parked.reason is not None and "answer or skip them" in parked.reason
        assert harness.run_states() == ["provisioning", "clarifying", "awaiting_answers"]
        assert harness.sandboxes_left() == []
        assert desk.failures == [] and desk.delivered == []
        ((run_id, questions),) = desk.asked
        assert run_id == parked.run_id
        assert [q.id for q in questions] == ["fmt", "who"]
        assert questions[0].choices[1].label == "PDF"
        (waiting,) = [e for e in harness.events if e.type == HostEventTypes.RUN_AWAITING_ANSWERS]
        assert waiting.data["plan_id"] == "plan_1" and waiting.data["node_id"] == "node_epic"
        assert [q["prompt"] for q in waiting.data["questions"]] == [
            "Which formats?",
            "Who downloads them?",
        ]
        record = harness.engine().store.get_run(parked.run_id)
        assert record.state == "awaiting_answers" and record.stage == "clarifying"

        # A person answers: a choice, and their own words. The repository
        # moved on meanwhile.
        desk.answer(fmt=PlanAnswer(value="pdf"), who=PlanAnswer(text="the finance team"))
        with open_repo(tmp_path / "upstream") as origin:
            commit_files(origin, {"README.md": "# app, with exports\n"}, "exports")
        harness.script([answer(code_task("c1"))])
        harness.events.clear()
        resumed = engine(harness, desk, keep_sandboxes=True).resume(parked.run_id)

        assert resumed.state == "completed", resumed.reason
        assert harness.consumed() == 1, "the questions are not asked a second time"
        # Re-entered where it parked, it finds its questions settled on the
        # record and goes straight to the proposal.
        assert harness.run_states() == ["provisioning", "proposing", "completed"]
        (prompt,) = [
            j["prompt"] for j in harness.agent_jobs(parked.run_id) if j["kind"] == "agent.session"
        ]
        assert "- **Which formats?**\n  Answer: PDF (`pdf`)" in prompt
        assert "- **Who downloads them?**\n  Answer: in their words: the finance team" in prompt
        assert len(desk.delivered) == 1 and len(desk.asked) == 1
        assert len(upstream) == 2, "a resume re-cuts the checkout: the planner reads today's tree"

    def test_a_skip_proposes_with_no_answers(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=2))
        harness.script([asks(question("fmt", "Which formats?", "csv", "pdf"))])
        parked = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert parked.state == "awaiting_answers"
        desk.answer(skip=True)
        harness.script([answer(code_task("c1"))])
        resumed = engine(harness, desk, keep_sandboxes=True).resume(parked.run_id)
        assert resumed.state == "completed", resumed.reason
        (prompt,) = [
            j["prompt"] for j in harness.agent_jobs(parked.run_id) if j["kind"] == "agent.session"
        ]
        assert "The person skipped these questions" in prompt
        assert "Answer: not answered" in prompt

    def test_a_resume_that_is_no_answer_parks_again_without_a_turn(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=2))
        harness.script([asks(question("fmt", "Which formats?", "csv", "pdf"))])
        parked = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        harness.script([])
        again = engine(harness, desk).resume(parked.run_id)
        assert again.state == "awaiting_answers"
        assert harness.consumed() == 0 and len(desk.asked) == 1
        assert desk.failures == []

    def test_the_cap_is_held_with_one_retry(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=1))
        two = asks(question("a", "A?", "x", "y"), question("b", "B?", "x", "y"))
        harness.script([two, asks(question("a", "A?", "x", "y"))])
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "awaiting_answers", result.reason
        retried = max(sessions(harness, result.run_id), key=len)
        assert "ask at most 1 question; this answer asks 2" in retried
        ((_, questions),) = desk.asked
        assert [q.id for q in questions] == ["a"]

    def test_questions_invalid_twice_fail_the_run_named(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=2))
        one_choice = asks(question("a", "A?", "only"))
        harness.script([one_choice, one_choice])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "failed"
        assert (result.reason or "").startswith("the planner's questions were invalid twice:")
        assert "give 2 to 5" in (result.reason or "")
        assert desk.failures == [(result.run_id, result.reason)] and desk.asked == []

    def test_no_questions_allowed_means_no_clarifying_turn(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=0))
        harness.script([answer(code_task("c1"))])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert "clarifying" not in harness.run_states()
        assert harness.consumed() == 1

    def test_an_earlier_generations_answers_are_not_asked_again(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        earlier = Clarification(
            run_id="an-earlier-run",
            questions=[
                PlanQuestion.model_validate(question("fmt", "Which formats?", "csv", "pdf"))
            ],
            answers={"fmt": PlanAnswer(value="csv")},
            status="answered",
        )
        desk = RecordingDesk(plan_brief=epic_brief(max_questions=2), clarification=earlier)
        harness.script([READY, answer(code_task("c1"))])
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        prompts = sessions(harness, result.run_id)
        clarify = next(p for p in prompts if p.startswith("# Before you propose"))
        propose = next(p for p in prompts if p.startswith("# Propose"))
        for prompt in (clarify, propose):
            assert "- **Which formats?**\n  Answer: CSV (`csv`)" in prompt


class TestClarificationShape:
    def test_questions_take_the_chat_choice_shape(self) -> None:
        parsed = PlanClarification.model_validate(
            {
                "questions": [
                    {"prompt": " Which\nformat? ", "choices": ["csv", {"label": "PDF"}]},
                    {"prompt": "Who?", "choices": [{"value": "a"}, {"value": "b"}]},
                ]
            }
        )
        first, second = parsed.questions
        assert (first.id, second.id) == ("q1", "q2"), "ids are minted in order"
        assert first.prompt == "Which format?"
        assert [(c.value, c.label) for c in first.choices] == [("csv", "csv"), ("PDF", "PDF")]
        assert first.allow_free_text is True

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"ready": True, "questions": [question("a", "A?", "x", "y")]},
            {"questions": [question("a", "A?", "x")]},
            {"questions": [question("a", "A?", *"abcdef")]},
            {"questions": [question("a", "A?", "x", "x")]},
            {"questions": [question("a", "A?", "x", "y"), question("a", "B?", "x", "y")]},
        ],
    )
    def test_malformed_answers_are_refused(self, body: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            PlanClarification.model_validate(body)

    def test_the_bounds_are_the_chat_choice_questions(self) -> None:
        from lantern.daemon import chat_choices
        from lantern.engine import planning

        assert (planning.MIN_CHOICES, planning.MAX_CHOICES) == (
            chat_choices.MIN_CHOICES,
            chat_choices.MAX_CHOICES,
        )


class TestThePrompt:
    def test_repositories_kept_children_target_are_named_not_checked_out(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        brief = epic_brief(
            level="initiative",
            child_level="epic",
            kept=["Mobile app"],
            repositories=["o/mobile"],
        )
        harness.script(
            [
                answer(
                    {
                        "id": "e1",
                        "title": "Reports API",
                        "goal": "reports download",
                        "context": "src/reports.py",
                        "acceptance_criteria": ["a report downloads"],
                    }
                )
            ]
        )
        built = engine(harness, RecordingDesk(plan_brief=brief), keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert len(upstream) == 1, "only the node's own repository is cloned"
        (prompt,) = [
            j["prompt"] for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"
        ]
        assert "Propose the epics of one initiative" in prompt
        assert "o/mobile is not checked out here" in prompt
        assert "- Mobile app" in prompt


class TestTheModel:
    def test_the_planner_runs_on_the_plan_model(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        harness.script([answer(code_task("c1"))])
        built = engine(
            harness,
            RecordingDesk(),
            keep_sandboxes=True,
            agent={"models": {"plan": "planner-model", "decompose": "decomposer-model"}},
        )
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        (job,) = [j for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"]
        assert job["model"] == "planner-model"


def verdict(decision: str, *reasons: str) -> dict[str, Any]:
    return {"json": {"verdict": decision, "reasons": list(reasons)}}


def reviewed_brief(**over: Any) -> PlanBrief:
    """A breakdown of a plan that advances itself, as the service briefs it."""
    return epic_brief(review=True, **over)


def _sessions(harness: Harness, run_id: str) -> list[dict[str, Any]]:
    """The run's agent sessions, the planner's before the reviewer's."""
    jobs = [j for j in harness.agent_jobs(run_id) if j["kind"] == "agent.session"]
    return sorted(jobs, key=lambda j: j["prompt"].startswith("# Review"))


class TestPlanReview:
    """The critic's turn between the proposal and its delivery, for a plan
    that advances itself: its verdict is delivered with the proposal, it
    fails closed, and it is never taken twice."""

    @pytest.mark.parametrize(
        ("decision", "reasons"),
        [("approve", ()), ("escalate", ("The second task repeats the first.",))],
    )
    def test_the_verdict_is_delivered_with_the_proposal(
        self,
        harness: Harness,
        upstream: list[tuple[str, str]],
        decision: str,
        reasons: tuple[str, ...],
    ) -> None:
        desk = RecordingDesk(plan_brief=reviewed_brief())
        harness.script(
            [answer(code_task("c1"), code_task("c2", deps=["c1"])), verdict(decision, *reasons)]
        )
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.consumed() == 2
        assert len(desk.delivered) == 1
        assert desk.reviews == [PlanVerdict(verdict=decision, reasons=list(reasons))]  # type: ignore[arg-type]
        # One row per turn, each charged, and the verdict on the task.
        rows = [(r.phase, r.status) for r in built.store.phase_attempts(result.run_id)]
        assert rows == [("propose", "ok"), ("plan_review", "ok")]
        (task,) = result.tasks
        assert task.output is not None
        assert task.output.data["review"] == {"verdict": decision, "reasons": list(reasons)}
        assert "proposal" in task.output.data
        # phase.end for the review; nothing of a code run's review.
        ends = [e for e in harness.events if e.type == HostEventTypes.PHASE_END]
        assert [e.data["phase"] for e in ends] == ["propose", "plan_review"]
        assert not [e for e in harness.events if e.type.startswith("review.")]
        # The reviewer read the level as it will be published, read-only.
        propose, review = _sessions(harness, result.run_id)
        assert propose["prompt"].startswith("# Propose the tasks of one epic")
        assert review["permission_mode"] == "read_only"
        prompt = review["prompt"]
        assert prompt.startswith("# Review the proposed tasks of one epic")
        assert "People can download their reports as CSV" in prompt, "the node's goal"
        assert "### 1. Task c1" in prompt and "### 2. Task c2" in prompt
        assert "#### Acceptance criteria\n\n- [ ] c1 works" in prompt, "the publish render"
        assert "#### Depends on\n\n- `1`" in prompt

    def test_an_unusable_verdict_twice_escalates_and_still_delivers(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=reviewed_brief())
        bad = verdict("maybe")
        harness.script([answer(code_task("c1")), bad, verdict("escalate")])
        built = engine(harness, desk)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.consumed() == 3, "one retry, as every structured answer gets"
        (review,) = desk.reviews
        assert review == PlanVerdict(
            verdict="escalate", reasons=["the reviewer did not return a usable verdict"]
        )
        rows = [(r.phase, r.status) for r in built.store.phase_attempts(result.run_id)]
        assert rows == [("propose", "ok"), ("plan_review", "failed")]
        (end,) = [
            e
            for e in harness.events
            if e.type == HostEventTypes.PHASE_END and e.data["phase"] == "plan_review"
        ]
        assert end.data["status"] == "failed"
        assert end.data["message"].startswith("the reviewer did not return a usable verdict")

    def test_a_resume_after_the_review_delivers_without_another_turn(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=reviewed_brief(), refuse="the plan is busy")
        harness.script([answer(code_task("c1")), verdict("approve")])
        first = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert first.state == "failed"
        desk.refuse = None
        harness.script([])
        harness.events.clear()
        resumed = engine(harness, desk).resume(first.run_id)
        assert resumed.state == "completed", resumed.reason
        assert harness.consumed() == 0, "neither the proposal nor the review is asked again"
        assert desk.reviews == [PlanVerdict(verdict="approve")]
        assert not [e for e in harness.events if e.type == HostEventTypes.PHASE_END]

    def test_a_resume_between_the_proposal_and_the_review_asks_only_the_reviewer(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=reviewed_brief())
        # The reviewer's job dies (the script runs out): the proposal is kept.
        harness.script([answer(code_task("c1"))])
        built = engine(harness, desk)
        with pytest.raises(WorkerError, match="echo script exhausted"):
            built.start("plan", repo=REPO, kind="plan")
        run_id = built.store.list_runs()[0].run_id
        assert desk.delivered == [], "a proposal never lands without its review"
        harness.script([verdict("escalate", "Too coarse to deliver in one run.")])
        resumed = engine(harness, desk, keep_sandboxes=True).resume(run_id)
        assert resumed.state == "completed", resumed.reason
        assert harness.consumed() == 1
        (session,) = _sessions(harness, run_id)
        assert session["prompt"].startswith("# Review the proposed")
        assert desk.reviews == [
            PlanVerdict(verdict="escalate", reasons=["Too coarse to deliver in one run."])
        ]

    def test_the_reviewer_is_the_critic_on_the_review_model(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        harness.script([answer(code_task("c1")), verdict("approve")])
        built = engine(
            harness,
            RecordingDesk(plan_brief=reviewed_brief()),
            keep_sandboxes=True,
            agent={"models": {"plan": "planner-model", "review": "review-model"}},
        )
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        propose, review = _sessions(harness, result.run_id)
        assert propose["model"] == "planner-model"
        assert review["model"] == "review-model"
        # The critic's briefing: a read-only session that judges.
        assert "You are a critic" in (review["system_message"] or "")

    def test_a_brief_without_review_never_reviews(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk()
        harness.script([answer(code_task("c1"))])
        built = engine(harness, desk)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.consumed() == 1
        assert desk.reviews == [None]
        rows = [r.phase for r in built.store.phase_attempts(result.run_id)]
        assert rows == ["propose"]

    def test_a_replan_is_not_reviewed(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        """The brief never asks it (a re-plan's diff waits for a person),
        and the engine would not review a diff if it did."""
        brief = reviewed_brief(
            mode="replan",
            current=[
                CurrentChild(
                    id="node_a", title="Export as CSV", state="published", origin="planner"
                )
            ],
        )
        desk = RecordingDesk(plan_brief=brief)
        harness.script([{"json": {"add": [], "modify": [], "suggest_close": []}}])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.consumed() == 1 and len(desk.replans) == 1


class TestVerdictShape:
    def test_escalate_says_why(self) -> None:
        with pytest.raises(ValueError, match="needs at least one reason"):
            PlanVerdict.model_validate({"verdict": "escalate", "reasons": []})

    def test_only_two_verdicts(self) -> None:
        with pytest.raises(ValueError):
            PlanVerdict.model_validate({"verdict": "approve with changes", "reasons": ["x"]})

    def test_reasons_are_folded_and_short(self) -> None:
        folded = PlanVerdict.model_validate({"verdict": "approve", "reasons": ["  a\n b ", " "]})
        assert folded.reasons == ["a b"]
        with pytest.raises(ValueError, match="under 500 characters"):
            PlanVerdict.model_validate({"verdict": "approve", "reasons": ["x" * 501]})
        with pytest.raises(ValueError):
            PlanVerdict.model_validate({"verdict": "approve", "reasons": ["x"] * 11})

    def test_an_unusable_review_stands_for_escalate(self) -> None:
        assert PlanVerdict.unusable().verdict == "escalate"


class TestProposalRules:
    def test_epics_carry_no_task_sections(self) -> None:
        brief = epic_brief(level="initiative", child_level="epic")
        whole = {"goal": "g", "context": "c", "acceptance_criteria": ["done"]}
        proposal = PlanProposal.model_validate(
            {
                "children": [
                    {"title": "API", "kind": "code", "depends_on": [2], **whole},
                    {"title": "UI", **whole},
                ]
            }
        )
        (problem,) = proposal_problems(proposal, brief)
        assert problem == "epic 1 (API): only a task carries kind, depends_on"

    def test_dependencies_by_position_and_cycles(self) -> None:
        proposal = PlanProposal.model_validate(
            {"children": [code_task("a", deps=[2]), code_task("b", deps=["a"])]}
        )
        problems = proposal_problems(proposal, epic_brief())
        assert any("cycle" in p for p in problems)
        ordered = PlanProposal.model_validate(
            {"children": [code_task("a"), code_task("b", deps=[1, "a"])]}
        )
        assert ordered.dependencies() == [[], [0]]

    def test_verify_commands_name_no_shell_variable(self) -> None:
        proposal = PlanProposal.model_validate(
            {"children": [code_task("a", verify_commands=["cd $APP_DIR && make test"])]}
        )
        (problem,) = proposal_problems(proposal, epic_brief())
        assert "uses a shell variable" in problem

    def test_verify_commands_are_linted_for_the_target(self) -> None:
        proposal = PlanProposal.model_validate({"children": [code_task("a")]})
        problems = proposal_problems(
            proposal, epic_brief(), lint=lambda commands: [f"`{c}` is bare" for c in commands]
        )
        assert problems == ["task a (Task a): `make test` is bare"]


class TestEveryChildIsWhole:
    """What the prompt asks of every child — a title, a goal, context and
    acceptance criteria — the validator holds it to, for epics as much as
    tasks, so a thin child is sent back once rather than delivered."""

    def test_an_epic_needs_its_sections(self) -> None:
        brief = epic_brief(level="initiative", child_level="epic")
        proposal = PlanProposal.model_validate({"children": [{"title": "Reports API"}]})
        problems = proposal_problems(proposal, brief)
        assert "epic 1 (Reports API): every child needs a goal" in problems
        assert "epic 1 (Reports API): every child needs context" in problems
        assert any("at least one acceptance criterion" in p for p in problems)

    def test_a_task_needs_a_goal_and_context_too(self) -> None:
        proposal = PlanProposal.model_validate(
            {"children": [code_task("a", goal="", context="  ")]}
        )
        problems = proposal_problems(proposal, epic_brief())
        assert "task a (Task a): every child needs a goal" in problems
        assert "task a (Task a): every child needs context" in problems

    def test_a_child_that_stays_is_not_proposed_again(self) -> None:
        brief = epic_brief(kept=["Export as CSV"])
        proposal = PlanProposal.model_validate(
            {"children": [code_task("a", title="  export  as csv"), code_task("b", title="Task b")]}
        )
        (problem,) = proposal_problems(proposal, brief)
        assert "repeats a child that stays" in problem and "Export as CSV" in problem

    def test_two_children_with_one_title_are_sent_back(self) -> None:
        proposal = PlanProposal.model_validate(
            {"children": [code_task("a", title="Same"), code_task("b", title="same ")]}
        )
        (problem,) = proposal_problems(proposal, epic_brief())
        assert "task b (same): repeats task a" in problem

    def test_the_hosts_stamp_is_not_the_planners_to_set(self) -> None:
        proposal = PlanProposal.model_validate(
            {"children": [code_task("a")], "source_input": {"title": "x"}}
        )
        (problem,) = proposal_problems(proposal, epic_brief())
        assert "source_input" in problem and "leave it out" in problem


# -- a re-plan (#2346) -----------------------------------------------------------


def current_child(id: str, **over: Any) -> CurrentChild:
    fields: dict[str, Any] = {
        "id": id,
        "title": f"Current {id}",
        "state": "published",
        "origin": "planner",
        "issue": f"{REPO}#{id[-1]}",
        "forge_state": "open",
        "changeable": True,
        "owned": True,
        "goal": f"goal of {id}",
        "acceptance_criteria": [f"{id} works"],
        "kind": "code",
        "verify_commands": ["make test"],
    }
    return CurrentChild.model_validate(fields | over)


def replan_brief(**over: Any) -> PlanBrief:
    return epic_brief(
        mode="replan",
        room=2,
        cap=4,
        current=[
            current_child("node_1"),
            current_child("node_2"),
            current_child("node_3", forge_state="closed", changeable=False),
            current_child("node_4", origin="forge", owned=False),
        ],
        **over,
    )


def diff(**entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"json": {"add": [], "modify": [], "suggest_close": []} | entries}


class TestReplanRun:
    def test_a_replan_is_a_diff_against_the_current_children(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=replan_brief())
        harness.script(
            [
                diff(
                    add=[code_task("a1") | {"rationale": "the export needs a size limit"}],
                    modify=[
                        {
                            "target": "node_1",
                            "acceptance_criteria": ["node_1 works", "and is fast"],
                            "rationale": "the repository times this path",
                        }
                    ],
                    suggest_close=[{"target": "node_2", "rationale": "src/old.py is gone"}],
                )
            ]
        )
        fake = FakeGithub()
        built = engine(harness, desk, keep_sandboxes=True)
        built._github_ops = lambda client, run_id: fake
        result = built.start("Re-plan the tasks of “Export reports”", repo=REPO, kind="plan")

        assert result.state == "completed", result.reason
        assert desk.briefs[-1] is True and desk.briefs.count(True) == 1, (
            "the planner is given a fresh brief"
        )
        ((run_id, replan),) = desk.replans
        assert run_id == result.run_id and desk.delivered == []
        assert [c.title for c in replan.add] == ["Task a1"]
        assert replan.modify[0].changes() == {
            "acceptance_criteria": ["node_1 works", "and is fast"]
        }
        assert [c.target for c in replan.suggest_close] == ["node_2"]
        # The planner read every current child by its id, and was told what
        # it may do with each.
        (job,) = [j for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"]
        prompt = job["prompt"]
        assert prompt.startswith("# Re-plan the tasks of one epic")
        assert "### `node_1` — Current node_1" in prompt
        assert f"{REPO}#1, open; may be changed or closed." in prompt
        assert "closed or not followed: leave it" in prompt
        assert "filed by a person in their own words: may be closed, not rewritten" in prompt
        assert job["permission_mode"] == "read_only"
        (task,) = result.tasks
        assert task.output is not None
        assert task.output.summary == (
            "Re-planned the epic “Export reports”: 1 to add, 1 to change, 1 to close; "
            "the diff waits in the plan for review"
        )
        assert task.output.data["replan"]["suggest_close"][0]["target"] == "node_2"
        ((entry),) = result.published
        assert entry.location == "plan plan_1/node_epic (re-plan)"
        assert fake.issues_created == [] and fake.raw_calls == [], "nothing reached the forge"

    def test_an_addition_that_repeats_a_current_child_is_sent_back(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=replan_brief())
        repeat = code_task("a1", title="current NODE_1")
        harness.script(
            [
                diff(add=[repeat]),
                diff(modify=[{"target": "node_1", "goal": "sharper", "rationale": "r"}]),
            ]
        )
        built = engine(harness, desk, keep_sandboxes=True)
        result = built.start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        assert harness.consumed() == 2
        jobs = [j for j in harness.agent_jobs(result.run_id) if j["kind"] == "agent.session"]
        retried = max(jobs, key=lambda j: len(j["prompt"]))["prompt"]
        assert "repeats the current child node_1 (“Current node_1”)" in retried
        assert "never add a child that exists" in retried
        ((_, replan),) = desk.replans
        assert replan.add == [] and [m.target for m in replan.modify] == ["node_1"]

    def test_an_empty_diff_is_an_answer(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=replan_brief())
        harness.script([diff()])
        result = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert result.state == "completed", result.reason
        ((_, replan),) = desk.replans
        assert replan.count == 0
        (task,) = result.tasks
        assert task.output is not None and "nothing is proposed" in task.output.summary

    def test_a_resume_after_the_turn_delivers_the_diff_without_asking_again(
        self, harness: Harness, upstream: list[tuple[str, str]]
    ) -> None:
        desk = RecordingDesk(plan_brief=replan_brief(), refuse="the plan is busy")
        harness.script([diff(suggest_close=[{"target": "node_1", "rationale": "done elsewhere"}])])
        first = engine(harness, desk).start("plan", repo=REPO, kind="plan")
        assert first.state == "failed"
        desk.refuse = None
        harness.script([])
        resumed = engine(harness, desk).resume(first.run_id)
        assert resumed.state == "completed", resumed.reason
        assert harness.consumed() == 0
        ((_, replan),) = desk.replans
        assert [c.target for c in replan.suggest_close] == ["node_1"]


class TestReplanRules:
    def _problems(self, **entries: list[dict[str, Any]]) -> list[str]:
        replan = PlanReplan.model_validate({"add": [], "modify": [], "suggest_close": []} | entries)
        return replan_problems(replan, replan_brief())

    def test_a_good_diff_has_none(self) -> None:
        assert (
            self._problems(
                add=[code_task("a1", deps=["node_1"], rationale="nothing exports yet")],
                modify=[{"target": "node_1", "title": "Sharper", "rationale": "r"}],
                suggest_close=[{"target": "node_4", "rationale": "covered by node_1"}],
            )
            == []
        )

    def test_an_addition_is_never_a_child_that_exists(self) -> None:
        (problem,) = self._problems(add=[code_task("node_2", title="Brand new", rationale="r")])
        assert "`node_2` is a current child's id; `modify` it rather than add it" in problem
        (problem,) = self._problems(
            add=[code_task("a1", title="  current   node_2 ", rationale="r")]
        )
        assert "repeats the current child node_2" in problem

    def test_the_room_left_by_the_cap(self) -> None:
        problems = self._problems(add=[code_task(i, rationale="r") for i in ("a1", "a2", "a3")])
        assert "add at most 2 tasks (the level's cap is 4); this answer adds 3" in problems

    def test_an_entry_names_a_changeable_current_child_once(self) -> None:
        problems = self._problems(
            modify=[
                {"target": "node_9", "goal": "x"},
                {"target": "node_3", "goal": "x"},
                {"target": "node_4", "goal": "x"},
                {"target": "node_1"},
            ],
            suggest_close=[{"target": "node_1", "rationale": "gone"}],
        )
        assert any("modify node_9: no current child has that id" in p for p in problems)
        assert any(
            "modify node_3: “Current node_3” is closed or not followed" in p for p in problems
        )
        assert any("a person filed on the forge in their own words" in p for p in problems)
        assert "modify node_1: name at least one section to change" in problems
        assert any(
            "suggest_close node_1: node_1 is already in this diff (modify)" in p for p in problems
        )

    def test_a_change_is_judged_on_the_child_it_makes(self) -> None:
        problems = self._problems(
            modify=[{"target": "node_1", "kind": "workload", "workload_profile": "nowhere"}]
        )
        assert any("workload profile `nowhere` is not configured" in p for p in problems)
        problems = self._problems(modify=[{"target": "node_1", "depends_on": ["node_1", "a1"]}])
        assert "modify node_1: a child cannot depend on itself" in problems
        assert any("depends on 'a1', which is not a current child" in p for p in problems)

    def test_an_addition_depends_on_an_addition_or_a_current_child(self) -> None:
        problems = self._problems(add=[code_task("a1", deps=["a2"]), code_task("a2", deps=["a1"])])
        assert any("those dependencies make a cycle" in p for p in problems)
        problems = self._problems(add=[code_task("a1", deps=["node_x"])])
        assert any("neither an addition nor a current child" in p for p in problems)

    def test_a_close_says_why(self) -> None:
        with pytest.raises(ValueError, match="say why"):
            PlanReplan.model_validate({"suggest_close": [{"target": "node_1", "rationale": " "}]})

    def test_every_entry_says_why(self) -> None:
        problems = self._problems(
            add=[code_task("a1", rationale=" ")],
            modify=[{"target": "node_1", "title": "Sharper"}],
        )
        assert "addition a1 (Task a1): say why in `rationale`" in problems
        assert "modify node_1: say why in `rationale`" in problems

    def test_changes_cannot_make_a_dependency_cycle(self) -> None:
        # node_2 already depends on node_1; making node_1 depend on node_2 loops.
        replan = PlanReplan.model_validate(
            {"modify": [{"target": "node_1", "depends_on": ["node_2"], "rationale": "r"}]}
        )
        brief = epic_brief(
            mode="replan",
            room=2,
            cap=4,
            current=[
                current_child("node_1"),
                current_child("node_2", depends_on=["node_1"]),
            ],
        )
        problems = replan_problems(replan, brief)
        assert any("make a cycle" in p for p in problems), problems

    def test_an_addition_does_not_depend_on_a_child_this_diff_closes(self) -> None:
        problems = self._problems(
            add=[code_task("a1", deps=["node_2"], rationale="r")],
            suggest_close=[{"target": "node_2", "rationale": "gone"}],
        )
        assert any("depends on node_2, which this diff closes" in p for p in problems), problems

    def test_a_change_may_not_empty_a_required_section(self) -> None:
        problems = self._problems(
            modify=[{"target": "node_1", "goal": "", "acceptance_criteria": [], "rationale": "r"}]
        )
        assert "modify node_1: a child cannot be left without a goal" in problems
        assert any("without acceptance criteria" in p for p in problems)
