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

from sbxloop import hostgit
from sbxloop.engine.model import PLAN_STAGES, RESUMABLE_RUN_STATES
from sbxloop.engine.planning import (
    PlanBrief,
    PlanDelivery,
    PlanProposal,
    ProfileRef,
    proposal_problems,
)
from sbxloop.errors import ConfigError, PlanDeliveryError
from sbxloop.events import HostEventTypes
from sbxloop.sbx.naming import run_name
from tests.conftest import FakeSbx
from tests.fakes.fake_github import FakeGithub
from tests.fakes.gitrepo import make_repo
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
        "acceptance_criteria": ["a summary lists three formats"],
        "kind": "workload",
        "workload_profile": profile,
    }


def answer(*children: dict[str, Any]) -> dict[str, Any]:
    return {"json": {"children": list(children)}}


@dataclass
class RecordingDesk:
    """A plan record that remembers what it was told."""

    plan_brief: PlanBrief = field(default_factory=epic_brief)
    started_runs: list[str] = field(default_factory=list)
    delivered: list[tuple[str, PlanProposal]] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    refuse: str | None = None

    def brief(self) -> PlanBrief:
        return self.plan_brief

    def started(self, run_id: str) -> None:
        self.started_runs.append(run_id)

    def deliver(self, run_id: str, proposal: PlanProposal) -> PlanDelivery:
        if self.refuse is not None:
            raise PlanDeliveryError(self.refuse)
        self.delivered.append((run_id, proposal))
        return PlanDelivery(len(proposal.children), f"plan plan_1/{self.plan_brief.node_id}")

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

    def test_the_stage_is_resumable_and_named(self) -> None:
        assert PLAN_STAGES == ("proposing",)
        assert "proposing" in RESUMABLE_RUN_STATES

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
        assert "a task needs at least one acceptance criterion" in retried
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
        harness.script([answer({"id": "e1", "title": "Reports API"})])
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


class TestProposalRules:
    def test_epics_carry_no_task_sections(self) -> None:
        brief = epic_brief(level="initiative", child_level="epic")
        proposal = PlanProposal.model_validate(
            {"children": [{"title": "API", "kind": "code", "depends_on": [2]}, {"title": "UI"}]}
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
