"""A ``plan`` run's shapes: what the planner is asked, what it may answer,
and the desk the answer is delivered to.

A plan run proposes one level of a plan — an initiative's epics, or an
epic's tasks — from a read-only checkout of the repository, and delivers
the proposal to the plan record, never to the forge. The engine knows the
plan only through a :class:`PlanDesk`: the brief it reads before the turn,
and the record it hands the validated proposal to. The daemon's desk is
the plan service; a test's is a list.

The proposal is held to the same rules a person's edit is (a task carries
acceptance criteria and a kind, a workload task names a configured profile,
a code task's verify commands are authored the way the in-run decompose
authors them), and to the level's cap, before anything is written: an
answer that breaks one is sent back once with the problems quoted, the way
``decompose`` retries, and a second bad answer fails the run named.

Before it proposes, the planner may ask (#2345): given the node, the
answers a person already gave and the checkout, it says it is ``ready`` or
asks up to ``[planning] max_questions`` questions, each in the shape a chat
choice question has (two to five choices, and free text unless it says
otherwise), so every client renders them the way chat already does. The
questions go to the plan record, the run parks ``awaiting_answers``, and a
person's answers — or their skip — ride into the proposal's prompt.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from sbxloop.engine.model import TaskSpec

#: The one task a plan run carries: the proposal is its work, its output
#: holds the validated answer, so a resume after the turn delivers without
#: asking again.
PROPOSE_TASK_ID = "propose"

#: The sink a plan run's result goes to: the plan record.
PLAN_SINK = "plan"

#: A clarifying question's choices: at least two, at most five — the bounds
#: of a chat choice question (``daemon.chat_choices``), so every bridge can
#: render one as buttons.
MIN_CHOICES = 2
MAX_CHOICES = 5

#: Where a clarification stands on the plan record: asked and waiting, or
#: settled by a person's answers, by their skip, or withdrawn because the
#: run that asked was given up before anyone answered.
ClarificationStatus = Literal["awaiting_answers", "answered", "skipped", "withdrawn"]

ParentLevel = Literal["initiative", "epic"]
ChildLevel = Literal["epic", "task"]

# A shell variable or substitution: `$NAME`, `${…}`, `$(…)`. `$?` alone is
# the exit status of the previous command and names nothing in the caller's
# environment.
_SHELL_VARIABLE = re.compile(r"\$(?:\{|\(|[A-Za-z_])")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileRef(_Model):
    """A configured workload profile, as the planner may name it."""

    name: str
    description: str = ""


def _fold(value: object) -> str:
    return " ".join(str(value or "").split())


class QuestionChoice(_Model):
    """One answer a person may pick: its ``value`` is what is recorded, its
    ``label`` what the button says."""

    value: str = Field(max_length=200)
    label: str = Field(default="", max_length=200)
    description: str | None = Field(default=None, max_length=300)

    @model_validator(mode="before")
    @classmethod
    def _from_text(cls, data: object) -> object:
        # A bare string is a choice whose value and label are the same; so
        # is a choice that names only one of the two.
        if isinstance(data, str):
            return {"value": data}
        if isinstance(data, dict) and data.get("value") is None and data.get("label"):
            return {**data, "value": data["label"]}
        return data

    @model_validator(mode="after")
    def _folded(self) -> QuestionChoice:
        self.value = _fold(self.value)
        self.label = _fold(self.label) or self.value
        self.description = _fold(self.description) or None
        if not self.value:
            raise ValueError("every choice needs a value")
        return self


class PlanQuestion(_Model):
    """One clarifying question, in the chat choice question's shape."""

    id: str = Field(default="", max_length=40)
    prompt: str = Field(max_length=1000)
    choices: list[QuestionChoice]
    allow_free_text: bool = True

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        folded = " ".join(value.split())
        if not folded:
            raise ValueError("every question needs a prompt")
        return folded

    @model_validator(mode="after")
    def _choices(self) -> PlanQuestion:
        values = [choice.value for choice in self.choices]
        if len(set(values)) != len(values):
            raise ValueError(f"question {self.id or self.prompt!r} repeats a choice: {values}")
        if not MIN_CHOICES <= len(self.choices) <= MAX_CHOICES:
            raise ValueError(
                f"question {self.id or self.prompt!r} has {len(self.choices)} choices; "
                f"give {MIN_CHOICES} to {MAX_CHOICES}"
            )
        return self

    def choice(self, value: str) -> QuestionChoice | None:
        return next((c for c in self.choices if c.value == value), None)


class PlanClarification(_Model):
    """The planner's clarifying answer: ``ready``, or the questions it needs
    a person to answer before it proposes."""

    ready: bool = False
    questions: list[PlanQuestion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_or_the_other(self) -> PlanClarification:
        if self.ready and self.questions:
            raise ValueError("answer `ready` or ask questions, not both")
        if not self.ready and not self.questions:
            raise ValueError("answer `ready: true`, or ask at least one question")
        seen: set[str] = set()
        for index, question in enumerate(self.questions, start=1):
            question.id = _fold(question.id) or f"q{index}"
            if question.id in seen:
                raise ValueError(f"question ids must be unique: {question.id!r} repeats")
            seen.add(question.id)
        return self


class PlanAnswer(_Model):
    """A person's answer to one question: the ``value`` of a choice they
    picked, or ``text`` in their own words (a question that allows it)."""

    value: str | None = None
    text: str = ""


class Clarification(_Model):
    """What the plan record holds of one generation's questions: which run
    asked them, what was asked, and what a person answered. The engine
    reads it from the brief; the plan service writes it."""

    run_id: str
    questions: list[PlanQuestion]
    answers: dict[str, PlanAnswer] = Field(default_factory=dict)
    status: ClarificationStatus = "awaiting_answers"
    asked_at: float = 0.0
    answered_at: float | None = None
    #: Who answered or skipped, as a person reads it.
    answered_by: str | None = None

    @property
    def settled(self) -> bool:
        """A person answered or skipped: the planner may go on."""
        return self.status in ("answered", "skipped")

    def unanswered(self) -> list[PlanQuestion]:
        return [q for q in self.questions if q.id not in self.answers]


def clarification_problems(answer: PlanClarification, brief: PlanBrief) -> list[str]:
    """Every rule the clarifying answer breaks, fed back verbatim; empty
    when it may be recorded."""
    count = len(answer.questions)
    if count > brief.max_questions:
        cap = brief.max_questions
        return [f"ask at most {cap} question{'s' if cap != 1 else ''}; this answer asks {count}"]
    return []


class PlanBrief(_Model):
    """What one plan run is asked: the node, the level under it, the room
    the level's cap leaves, the children that stay, the workload profiles a
    task may name, and the repositories to read. Read from the plan record
    when the run proposes — never persisted with the run, because the plan
    is the record."""

    plan_id: str
    node_id: str
    level: ParentLevel
    child_level: ChildLevel
    repository: str
    title: str
    goal: str = ""
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: str = ""
    constraints: str = ""
    #: The node above this one, when there is one (an epic's initiative),
    #: as a line of context.
    parent: str = ""
    #: The children that stay whatever is proposed (a person's drafts and
    #: approvals), by title.
    kept: list[str] = Field(default_factory=list)
    #: How many children this proposal may hold: the level's cap less the
    #: children that stay.
    room: int = Field(ge=1)
    #: The level's cap itself, for the prompt.
    cap: int = Field(ge=1)
    profiles: list[ProfileRef] = Field(default_factory=list)
    #: Repositories beyond ``repository`` that children which stay already
    #: target (an initiative's epics may live elsewhere): named to the
    #: planner, not checked out — the run's config holds one repository.
    repositories: list[str] = Field(default_factory=list)
    #: The person's note for this breakdown, when they wrote one.
    note: str = ""
    #: How many clarifying questions the planner may ask before it
    #: proposes (`[planning] max_questions` for the node's repository); 0
    #: skips the clarifying turn.
    max_questions: int = Field(default=0, ge=0)
    #: The node's latest clarifying questions and a person's answers — this
    #: run's, or an earlier generation's the planner should not ask again.
    clarification: Clarification | None = None

    def clarification_for(self, run_id: str) -> Clarification | None:
        """The questions ``run_id`` itself asked, when it asked any."""
        found = self.clarification
        return found if found is not None and found.run_id == run_id else None

    @property
    def child_noun(self) -> str:
        return "epics" if self.child_level == "epic" else "tasks"

    def task_title(self) -> str:
        return f"Propose the {self.child_noun} of “{self.title}”"


def plan_task(brief: PlanBrief) -> TaskSpec:
    """The seeded task a plan run carries."""
    return TaskSpec(
        id=PROPOSE_TASK_ID,
        title=brief.task_title(),
        description=(
            f"Read {brief.repository} and propose at most {brief.room} "
            f"{brief.child_noun} for the {brief.level} “{brief.title}”."
        ),
    )


def _text(value: object) -> str:
    """A section a person reads as prose; a list answer is read as bullets."""
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        items = [" ".join(str(item).split()) for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in items)
    return str(value).strip()


class ProposedChild(_Model):
    """One proposed child: the sections a plan node carries. ``id`` is a
    temporary name siblings' ``depends_on`` may use; it never reaches the
    plan, which mints its own."""

    id: str | None = None
    title: str
    goal: str = ""
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    kind: Literal["code", "workload"] | None = None
    workload_profile: str | None = None
    verify_commands: list[str] = Field(default_factory=list)
    depends_on: list[str | int] = Field(default_factory=list)
    non_goals: str = ""
    constraints: str = ""

    @field_validator("title")
    @classmethod
    def _fold_title(cls, value: str) -> str:
        folded = " ".join(str(value).split())
        if not folded:
            raise ValueError("every child needs a title")
        return folded

    @field_validator("non_goals", "constraints", "goal", "context", mode="before")
    @classmethod
    def _prose(cls, value: object) -> str:
        return _text(value)

    @field_validator("acceptance_criteria", "verify_commands")
    @classmethod
    def _strip_items(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

    @field_validator("workload_profile")
    @classmethod
    def _blank_profile(cls, value: str | None) -> str | None:
        return (value or "").strip() or None


class PlanProposal(_Model):
    """The planner's answer: the children, in the order a person reads them."""

    children: list[ProposedChild] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> PlanProposal:
        ids = [child.id for child in self.children if child.id]
        if len(set(ids)) != len(ids):
            raise ValueError(f"child ids must be unique: {ids}")
        return self

    def dependencies(self) -> list[list[int]]:
        """Each child's dependencies as positions among its siblings.

        ``depends_on`` names a sibling by its ``id``, or by its position (the
        first child is 1). A name that is no sibling, a child depending on
        itself, and a cycle are refused, named."""
        by_id: dict[str, int] = {
            child.id: index for index, child in enumerate(self.children) if child.id
        }
        resolved: list[list[int]] = []
        problems: list[str] = []
        for index, child in enumerate(self.children):
            label = child.id or f"child {index + 1}"
            deps: list[int] = []
            for ref in child.depends_on:
                target = _resolve(ref, by_id, len(self.children))
                if target is None:
                    problems.append(f"{label} depends on {ref!r}, which is not a sibling")
                elif target == index:
                    problems.append(f"{label} depends on itself")
                elif target not in deps:
                    deps.append(target)
            resolved.append(deps)
        if problems:
            raise ValueError("; ".join(problems))
        try:
            TopologicalSorter({i: set(deps) for i, deps in enumerate(resolved)}).prepare()
        except CycleError as exc:
            raise ValueError(f"those dependencies make a cycle: {exc.args[1]}") from exc
        return resolved


def _resolve(ref: str | int, by_id: dict[str, int], count: int) -> int | None:
    if isinstance(ref, str):
        key = ref.strip()
        if key in by_id:
            return by_id[key]
        if not key.isdigit():
            return None
        ref = int(key)
    return ref - 1 if 1 <= ref <= count else None


def proposal_problems(
    proposal: PlanProposal,
    brief: PlanBrief,
    *,
    lint: Callable[[Sequence[str]], list[str]] | None = None,
) -> list[str]:
    """Every rule the proposal breaks, as sentences fed back to the planner
    verbatim; empty when it may be delivered.

    ``lint`` is the verify-command lint for the target repository's
    toolchains (the one the in-run decompose is held to); None skips it."""
    problems: list[str] = []
    count = len(proposal.children)
    if count > brief.room:
        problems.append(f"propose at most {brief.room} {brief.child_noun}; this answer has {count}")
    profiles = {profile.name for profile in brief.profiles}
    for index, child in enumerate(proposal.children):
        label = f"{brief.child_level} {child.id or index + 1} ({child.title})"
        if brief.child_level == "epic":
            stray = [
                name
                for name, present in (
                    ("kind", child.kind is not None),
                    ("workload_profile", child.workload_profile is not None),
                    ("verify_commands", bool(child.verify_commands)),
                    ("depends_on", bool(child.depends_on)),
                )
                if present
            ]
            if stray:
                problems.append(f"{label}: only a task carries {', '.join(stray)}")
            continue
        if not child.acceptance_criteria:
            problems.append(f"{label}: a task needs at least one acceptance criterion")
        if child.kind is None:
            problems.append(f"{label}: a task needs a kind, `code` or `workload`")
        elif child.kind == "workload":
            if child.verify_commands:
                problems.append(
                    f"{label}: a workload task has no verify commands; its acceptance "
                    "criteria are its exam"
                )
            if child.workload_profile is None:
                problems.append(f"{label}: a workload task names the profile it runs under")
            elif child.workload_profile not in profiles:
                known = ", ".join(sorted(profiles)) or "none is configured"
                problems.append(
                    f"{label}: workload profile `{child.workload_profile}` is not configured "
                    f"(configured: {known})"
                )
        else:
            if child.workload_profile is not None:
                problems.append(f"{label}: only a workload task names a workload profile")
            if not child.verify_commands:
                problems.append(
                    f"{label}: a code task needs at least one verify command that exits 0 "
                    "only when the task is done"
                )
            for command in child.verify_commands:
                if _SHELL_VARIABLE.search(command):
                    problems.append(
                        f"{label}: verify command `{command}` uses a shell variable; it runs "
                        "from the workspace root of a later run whose environment this plan "
                        "cannot see, so name every path and value outright"
                    )
            if lint is not None:
                problems.extend(f"{label}: {message}" for message in lint(child.verify_commands))
    try:
        proposal.dependencies()
    except ValueError as exc:
        problems.append(str(exc))
    return problems


@dataclass(frozen=True, slots=True)
class PlanDelivery:
    """What the plan record took: how many children, and where."""

    count: int
    location: str


class PlanDesk(Protocol):
    """The plan record one plan run reads its brief from and delivers to.

    Every method answers for the run's own node. ``brief``, ``ask`` and
    ``deliver`` raise :class:`~sbxloop.errors.PlanDeliveryError` with a sentence a
    person reads when the record will not serve (the plan was deleted, the
    node was published under the run); ``started`` and ``failed`` are
    notices and never fail the run.
    """

    def brief(self) -> PlanBrief: ...

    def started(self, run_id: str) -> None: ...

    def ask(self, run_id: str, questions: Sequence[PlanQuestion]) -> None:
        """Record the planner's questions for a person to answer; the run
        parks until they do. Raises
        :class:`~sbxloop.errors.PlanDeliveryError` when the record will not
        take them."""
        ...

    def deliver(self, run_id: str, proposal: PlanProposal) -> PlanDelivery: ...

    def failed(self, run_id: str, reason: str) -> None: ...
