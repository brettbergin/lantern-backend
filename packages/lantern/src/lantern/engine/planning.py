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

A **re-plan** (#2346) is the same run on a node that is on the forge and
already has children there: the brief carries those children (as the
forge last had them), and the answer is a :class:`PlanReplan` — a diff of
``add``, ``modify`` and ``suggest_close`` entries against them, never a
replacement. A child is named by its node id, and an ``add`` that repeats
a child that exists is sent back, so nothing the forge has is proposed
twice. The diff waits on the plan for a person's approval.

A breakdown of a plan that advances itself is **reviewed** before it is
delivered: the brief says so (``review``), the critic judges the proposed
level once and answers a :class:`PlanVerdict` — ``approve`` or
``escalate`` with its reasons — and the verdict is delivered with the
proposal, in the same write. A reviewer whose answer is unusable twice
stands for ``escalate``: a failed review never approves.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lantern.engine.model import TaskSpec

#: The one task a plan run carries: the proposal is its work, its output
#: holds the validated answer, so a resume after the turn delivers without
#: asking again.
PROPOSE_TASK_ID = "propose"

#: Where the proposal task's output keeps a reviewed breakdown's verdict,
#: beside the proposal, so a resume delivers both without a second turn.
PLAN_REVIEW_KEY = "review"

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
#: What a plan run is asked for: a node's next level, or a diff against the
#: children a published node already has.
PlanMode = Literal["breakdown", "replan"]

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
    #: Where each question was posted in chat — ``<backend>:<message id>``
    #: → the question's id — so a reply to that message, or a click on its
    #: buttons, still finds its question after a restart has emptied the
    #: bridge's own memory of what it posted.
    posts: dict[str, str] = Field(default_factory=dict)

    @property
    def settled(self) -> bool:
        """A person answered or skipped: the planner may go on."""
        return self.status in ("answered", "skipped")

    def unanswered(self) -> list[PlanQuestion]:
        return [q for q in self.questions if q.id not in self.answers]

    def question(self, question_id: str) -> PlanQuestion | None:
        return next((q for q in self.questions if q.id == question_id), None)

    def posted(self, key: str) -> PlanQuestion | None:
        """The question posted as ``key`` (``<backend>:<message id>``)."""
        question_id = self.posts.get(key)
        return None if question_id is None else self.question(question_id)


def clarification_problems(answer: PlanClarification, brief: PlanBrief) -> list[str]:
    """Every rule the clarifying answer breaks, fed back verbatim; empty
    when it may be recorded."""
    count = len(answer.questions)
    if count > brief.max_questions:
        cap = brief.max_questions
        return [f"ask at most {cap} question{'s' if cap != 1 else ''}; this answer asks {count}"]
    return []


class CurrentChild(_Model):
    """A child a re-planned node already has, as the planner reads it: its
    node id (the name a ``modify`` or ``suggest_close`` targets), where it
    is on the forge, and its sections."""

    id: str
    title: str
    state: str
    origin: str
    #: ``owner/name#N`` when it is on the forge.
    issue: str = ""
    #: ``open`` or ``closed`` as the forge last said; ``None`` off the forge.
    forge_state: str | None = None
    #: On the forge, followed and open: a diff may change or close it.
    changeable: bool = False
    #: Its body carries the plan's rendered sections (a child a person
    #: filed on the forge has only their own text): a ``modify`` may
    #: rewrite them.
    owned: bool = True
    goal: str = ""
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    kind: Literal["code", "workload"] | None = None
    workload_profile: str | None = None
    verify_commands: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    non_goals: str = ""
    constraints: str = ""


class PlanBrief(_Model):
    """What one plan run is asked: the node, the level under it, the room
    the level's cap leaves, the children that stay, the workload profiles a
    task may name, and the repositories to read. Read from the plan record
    when the run proposes — never persisted with the run, because the plan
    is the record. A ``replan`` brief also carries the node's current
    children, and its answer is a :class:`PlanReplan`."""

    mode: PlanMode = "breakdown"
    input: dict[str, Any] = Field(default_factory=dict)
    generate_root: bool = False

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
    #: How many children this proposal may hold (a re-plan: how many it may
    #: add): the level's cap less the children that stay.
    room: int = Field(ge=0)
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
    #: Whether an independent reviewer judges the proposal before it is
    #: delivered (a breakdown of a plan that advances itself). False keeps
    #: the run exactly as it was: the planner proposes, a person decides.
    review: bool = False

    def clarification_for(self, run_id: str) -> Clarification | None:
        """The questions ``run_id`` itself asked, when it asked any."""
        found = self.clarification
        return found if found is not None and found.run_id == run_id else None

    #: A re-plan's current children, in the order a person reads them.
    current: list[CurrentChild] = Field(default_factory=list)

    @property
    def child_noun(self) -> str:
        return "epics" if self.child_level == "epic" else "tasks"

    def task_title(self) -> str:
        if self.mode == "replan":
            return f"Re-plan the {self.child_noun} of “{self.title}”"
        if self.generate_root:
            return (
                f"Generate the {self.level} and its {self.child_noun} "
                f"from “{self.input.get('title', '')}”"
            )
        return f"Propose the {self.child_noun} of “{self.title}”"

    def child(self, node_id: str) -> CurrentChild | None:
        return next((c for c in self.current if c.id == node_id), None)


def plan_task(brief: PlanBrief) -> TaskSpec:
    """The seeded task a plan run carries."""
    if brief.mode == "replan":
        description = (
            f"Read {brief.repository} and propose the changes the {brief.level} "
            f"“{brief.title}” needs to its {len(brief.current)} current "
            f"{brief.child_noun}: at most {brief.room} to add, and which to change or close."
        )
    else:
        description = (
            f"Read {brief.repository} and propose at most {brief.room} "
            f"{brief.child_noun} for the {brief.level} “{brief.title}”."
        )
    return TaskSpec(id=PROPOSE_TASK_ID, title=brief.task_title(), description=description)


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


class ProposedRoot(_Model):
    """Issue content authored from the intake brief, never a form echo."""

    title: str
    goal: str
    context: str
    acceptance_criteria: list[str]
    non_goals: str = ""
    constraints: str = ""

    def problems(self) -> list[str]:
        problems = []
        for name in ("title", "goal", "context"):
            if not getattr(self, name).strip():
                problems.append(f"the generated root needs {name}")
        if not self.acceptance_criteria or any(not a.strip() for a in self.acceptance_criteria):
            problems.append("the generated root needs nonempty acceptance criteria")
        return problems


class PlanProposal(_Model):
    """The planner's answer: the children, in the order a person reads them."""

    children: list[ProposedChild] = Field(default_factory=list)
    root: ProposedRoot | None = None
    #: Stamped by the host after inference, persisted for crash-safe delivery.
    source_input: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> PlanProposal:
        if not self.children and self.root is None:
            raise ValueError("a proposal needs children or a generated root")
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


def _child_problems(
    child: ProposedChild,
    label: str,
    brief: PlanBrief,
    lint: Callable[[Sequence[str]], list[str]] | None,
    *,
    whole: bool = True,
) -> list[str]:
    """Every rule one child of ``brief``'s level breaks. ``whole`` holds it
    to what the prompt asks of every child — a goal, context and
    acceptance criteria — which a change to an existing child is judged
    on only where it sets them (see :func:`_change_problems`)."""
    problems: list[str] = []
    if whole:
        for name, noun in (("goal", "a goal"), ("context", "context")):
            if not getattr(child, name).strip():
                problems.append(f"{label}: every child needs {noun}")
        if not child.acceptance_criteria:
            problems.append(f"{label}: every child needs at least one acceptance criterion")
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
        return problems
    profiles = {profile.name for profile in brief.profiles}
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
    return problems


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
    if not brief.generate_root and count == 0:
        problems.append("a breakdown needs at least one child")
    if brief.generate_root:
        if proposal.root is None:
            problems.append("generate the root issue as well as its children from the input brief")
        else:
            problems.extend(proposal.root.problems())
    if count > brief.room:
        problems.append(f"propose at most {brief.room} {brief.child_noun}; this answer has {count}")
    if proposal.source_input is not None:
        problems.append("`source_input` is the host's stamp, not part of the answer; leave it out")
    kept = {fold_title(title): title for title in brief.kept}
    seen: dict[str, str] = {}
    for index, child in enumerate(proposal.children):
        label = f"{brief.child_level} {child.id or index + 1} ({child.title})"
        folded = fold_title(child.title)
        if folded in kept:
            problems.append(
                f"{label} repeats a child that stays (“{kept[folded]}”); propose only what "
                "is still missing"
            )
        elif folded in seen:
            problems.append(f"{label}: repeats {seen[folded]}; one child per outcome")
        else:
            seen[folded] = label
        problems.extend(_child_problems(child, label, brief, lint))
    try:
        proposal.dependencies()
    except ValueError as exc:
        problems.append(str(exc))
    return problems


# -- a re-plan's diff (#2346) ----------------------------------------------------


def fold_title(title: str) -> str:
    """A title as two children are compared by: whitespace and case folded."""
    return " ".join(title.split()).casefold()


class ReplanAddition(ProposedChild):
    """A child the re-plan adds: a whole child, and why."""

    rationale: str = ""


#: The sections a ``modify`` may change, as a plan node names them.
CHANGEABLE = (
    "title",
    "goal",
    "context",
    "acceptance_criteria",
    "kind",
    "workload_profile",
    "verify_commands",
    "depends_on",
    "non_goals",
    "constraints",
)


class ReplanChange(_Model):
    """A change to one current child: ``target`` names it by node id, and
    only the sections given change (``null`` or absent leaves one as it
    is; an empty string or list empties it)."""

    target: str
    rationale: str = ""
    title: str | None = None
    goal: str | None = None
    context: str | None = None
    acceptance_criteria: list[str] | None = None
    kind: Literal["code", "workload"] | None = None
    workload_profile: str | None = None
    verify_commands: list[str] | None = None
    depends_on: list[str] | None = None
    non_goals: str | None = None
    constraints: str | None = None

    @field_validator("title")
    @classmethod
    def _fold_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        folded = " ".join(str(value).split())
        if not folded:
            raise ValueError("a changed title cannot be empty")
        return folded

    @field_validator("non_goals", "constraints", "goal", "context", mode="before")
    @classmethod
    def _prose(cls, value: object) -> str | None:
        return None if value is None else _text(value)

    @field_validator("acceptance_criteria", "verify_commands", "depends_on")
    @classmethod
    def _strip_items(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else [item.strip() for item in value if item.strip()]

    def changes(self) -> dict[str, Any]:
        """The sections this entry sets, by name."""
        return {key: getattr(self, key) for key in CHANGEABLE if getattr(self, key) is not None}


class ReplanClose(_Model):
    """A current child the planner thinks is no longer needed, and why."""

    target: str
    rationale: str

    @field_validator("rationale")
    @classmethod
    def _said(cls, value: str) -> str:
        text = " ".join(value.split())
        if not text:
            raise ValueError("say why the child is no longer needed")
        return text


class PlanReplan(_Model):
    """The planner's re-plan: a diff against the node's current children.
    Every list may be empty; an answer with none of them says the children
    already cover the node."""

    add: list[ReplanAddition] = Field(default_factory=list)
    modify: list[ReplanChange] = Field(default_factory=list)
    suggest_close: list[ReplanClose] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> PlanReplan:
        ids = [child.id for child in self.add if child.id]
        if len(set(ids)) != len(ids):
            raise ValueError(f"addition ids must be unique: {ids}")
        return self

    @property
    def count(self) -> int:
        return len(self.add) + len(self.modify) + len(self.suggest_close)

    def add_dependencies(self, current: Sequence[str]) -> list[list[int | str]]:
        """Each addition's dependencies: a position among the additions, or
        the node id of a current child. An addition's ``depends_on`` names a
        sibling addition by its ``id`` or position (the first addition is
        1), or a current child by its node id; anything else, a dependency
        on itself and a cycle among the additions are refused, named."""
        known = set(current)
        by_id = {child.id: index for index, child in enumerate(self.add) if child.id}
        resolved: list[list[int | str]] = []
        problems: list[str] = []
        for index, child in enumerate(self.add):
            label = child.id or f"addition {index + 1}"
            deps: list[int | str] = []
            for ref in child.depends_on:
                if isinstance(ref, str) and ref.strip() in known and ref.strip() not in by_id:
                    target: int | str | None = ref.strip()
                else:
                    target = _resolve(ref, by_id, len(self.add))
                if target is None:
                    problems.append(
                        f"{label} depends on {ref!r}, which is neither an addition nor a "
                        "current child"
                    )
                elif target == index:
                    problems.append(f"{label} depends on itself")
                elif target not in deps:
                    deps.append(target)
            resolved.append(deps)
        if problems:
            raise ValueError("; ".join(problems))
        graph = {i: {d for d in deps if isinstance(d, int)} for i, deps in enumerate(resolved)}
        try:
            TopologicalSorter(graph).prepare()
        except CycleError as exc:
            raise ValueError(f"those dependencies make a cycle: {exc.args[1]}") from exc
        return resolved


def replan_problems(
    replan: PlanReplan,
    brief: PlanBrief,
    *,
    lint: Callable[[Sequence[str]], list[str]] | None = None,
) -> list[str]:
    """Every rule a re-plan breaks, as sentences fed back to the planner
    verbatim; empty when it may be delivered. A child that exists is never
    added again: an addition whose title or ``id`` is a current child's is
    refused, and so is an entry naming no current child, a child changed or
    closed twice, and a change to a child that is closed, off the forge, or
    a person's own issue."""
    problems: list[str] = []
    noun = brief.child_noun
    if len(replan.add) > brief.room:
        problems.append(
            f"add at most {brief.room} {noun} (the level's cap is {brief.cap}); "
            f"this answer adds {len(replan.add)}"
        )
    titles = {fold_title(c.title): c for c in brief.current}
    ids = [c.id for c in brief.current]
    closing = {entry.target for entry in replan.suggest_close}
    for index, child in enumerate(replan.add):
        label = f"addition {child.id or index + 1} ({child.title})"
        if not child.rationale.strip():
            problems.append(f"{label}: say why in `rationale`")
        for ref in child.depends_on:
            if isinstance(ref, str) and ref.strip() in closing:
                problems.append(f"{label} depends on {ref.strip()}, which this diff closes")
        same = titles.get(fold_title(child.title))
        if same is not None:
            problems.append(
                f"{label} repeats the current child {same.id} (“{same.title}”); "
                "never add a child that exists — `modify` it by its id instead"
            )
        if child.id and child.id in ids:
            problems.append(
                f"{label}: `{child.id}` is a current child's id; `modify` it rather than add it"
            )
        problems.extend(_child_problems(child, label, brief, lint))
    try:
        replan.add_dependencies(ids)
    except ValueError as exc:
        problems.append(str(exc))
    seen: dict[str, str] = {}
    entries: list[tuple[ReplanChange | ReplanClose, str]] = [
        *((e, "modify") for e in replan.modify),
        *((e, "suggest_close") for e in replan.suggest_close),
    ]
    for entry, action in entries:
        target = brief.child(entry.target)
        label = f"{action} {entry.target}"
        if target is None:
            problems.append(f"{label}: no current child has that id ({', '.join(ids) or 'none'})")
            continue
        if entry.target in seen:
            problems.append(
                f"{label}: {entry.target} is already in this diff ({seen[entry.target]}); "
                "one entry per child"
            )
            continue
        seen[entry.target] = action
        if isinstance(entry, ReplanChange) and not entry.rationale.strip():
            problems.append(f"{label}: say why in `rationale`")
        if not target.changeable:
            problems.append(
                f"{label}: “{target.title}” is closed or not followed on the forge; leave it"
            )
            continue
        if isinstance(entry, ReplanChange):
            problems.extend(_change_problems(entry, target, brief, label, lint))
    # The level's dependencies as the diff leaves them — every current
    # child's, with each change's `depends_on` in place of what it had —
    # must still be an order to run in.
    graph: dict[str, set[str]] = {c.id: set(c.depends_on) for c in brief.current}
    for change in replan.modify:
        deps = change.changes().get("depends_on")
        if isinstance(deps, list) and change.target in graph:
            graph[change.target] = {d for d in deps if d in graph}
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        problems.append(f"those dependencies make a cycle among the current {noun}: {exc.args[1]}")
    return problems


def _change_problems(
    entry: ReplanChange,
    target: CurrentChild,
    brief: PlanBrief,
    label: str,
    lint: Callable[[Sequence[str]], list[str]] | None,
) -> list[str]:
    """What one ``modify`` breaks, judged on the child as it would be."""
    changes = entry.changes()
    if not target.owned:
        return [
            f"{label}: “{target.title}” is an issue a person filed on the forge in their own "
            "words; do not rewrite it (suggest closing it, or add what is missing)"
        ]
    if not changes:
        return [f"{label}: name at least one section to change"]
    problems: list[str] = []
    for name, noun in (("goal", "a goal"), ("context", "context")):
        if name in changes and not str(changes[name]).strip():
            problems.append(f"{label}: a child cannot be left without {noun}")
    if "acceptance_criteria" in changes and not changes["acceptance_criteria"]:
        problems.append(f"{label}: a child cannot be left without acceptance criteria")
    deps = changes.get("depends_on")
    if isinstance(deps, list):
        for dep in deps:
            if dep == target.id:
                problems.append(f"{label}: a child cannot depend on itself")
            elif brief.child(dep) is None:
                problems.append(
                    f"{label}: depends on {dep!r}, which is not a current child "
                    "(a change may depend only on children that exist)"
                )
    merged = ProposedChild(
        id=target.id,
        title=str(changes.get("title") or target.title),
        goal=target.goal,
        context=target.context,
        acceptance_criteria=list(changes.get("acceptance_criteria", target.acceptance_criteria)),
        kind=changes.get("kind", target.kind),
        workload_profile=changes.get("workload_profile", target.workload_profile),
        verify_commands=list(changes.get("verify_commands", target.verify_commands)),
        depends_on=[],
    )
    if brief.child_level == "epic":
        stray = [
            k for k in ("kind", "workload_profile", "verify_commands", "depends_on") if k in changes
        ]
        if stray:
            problems.append(f"{label}: only a task carries {', '.join(stray)}")
        return problems
    # A kind that changes to workload drops the verify commands it had, and
    # one that changes to code drops its profile, unless the entry says
    # otherwise.
    if merged.kind == "workload" and "verify_commands" not in changes and "kind" in changes:
        merged = merged.model_copy(update={"verify_commands": []})
    if merged.kind == "code" and "workload_profile" not in changes and "kind" in changes:
        merged = merged.model_copy(update={"workload_profile": None})
    checked = lint if "verify_commands" in changes else None
    problems.extend(_child_problems(merged, label, brief, checked, whole=False))
    return problems


# -- the reviewer's verdict on a proposed level ----------------------------------

#: The verdict a review that produced nothing usable stands for: never an
#: approval — a review that failed is a level a person has to look at.
REVIEW_UNUSABLE = "the reviewer did not return a usable verdict"
#: How many findings a verdict may carry, and how long each may be: a
#: person reads them, so they are short sentences, not a report.
MAX_REVIEW_REASONS = 10
MAX_REVIEW_REASON_CHARS = 500


class PlanVerdict(_Model):
    """The reviewer's answer on one proposed level: ``approve`` when it is a
    sound decomposition, ``escalate`` when a person should look (always
    with the reasons why), and the findings a person reads either way."""

    verdict: Literal["approve", "escalate"]
    reasons: list[str] = Field(default_factory=list, max_length=MAX_REVIEW_REASONS)

    @field_validator("reasons")
    @classmethod
    def _sentences(cls, value: list[str]) -> list[str]:
        folded = [" ".join(str(reason).split()) for reason in value]
        folded = [reason for reason in folded if reason]
        for reason in folded:
            if len(reason) > MAX_REVIEW_REASON_CHARS:
                raise ValueError(
                    f"keep each reason under {MAX_REVIEW_REASON_CHARS} characters; "
                    f"one has {len(reason)}"
                )
        return folded

    @model_validator(mode="after")
    def _said_why(self) -> PlanVerdict:
        if self.verdict == "escalate" and not self.reasons:
            raise ValueError("an `escalate` verdict needs at least one reason")
        return self

    @classmethod
    def unusable(cls) -> PlanVerdict:
        """The verdict a review stands for when the reviewer's answer was
        invalid twice: escalate, never approve."""
        return cls(verdict="escalate", reasons=[REVIEW_UNUSABLE])


@dataclass(frozen=True, slots=True)
class PlanDelivery:
    """What the plan record took: how many children, and where."""

    count: int
    location: str


class PlanDesk(Protocol):
    """The plan record one plan run reads its brief from and delivers to.

    Every method answers for the run's own node. ``brief``, ``ask``,
    ``deliver`` and ``deliver_replan`` raise
    :class:`~lantern.errors.PlanDeliveryError` with a sentence a person reads
    when the record will not serve (the plan was deleted, the node was
    published under the run); ``started`` and ``failed`` are notices and
    never fail the run. ``brief(fresh=True)`` is the brief the planner is
    about to be asked: a re-plan's children are read from the forge first
    (on the host, reading only), and a forge that cannot be read fails it
    named rather than re-plan against a stale tree.
    """

    def brief(self, *, fresh: bool = False) -> PlanBrief: ...

    def started(self, run_id: str) -> None: ...

    def ask(self, run_id: str, questions: Sequence[PlanQuestion]) -> None:
        """Record the planner's questions for a person to answer; the run
        parks until they do. Raises
        :class:`~lantern.errors.PlanDeliveryError` when the record will not
        take them."""
        ...

    def deliver(
        self, run_id: str, proposal: PlanProposal, *, review: PlanVerdict | None = None
    ) -> PlanDelivery:
        """Write the proposal under the node. ``review`` (a reviewed
        breakdown's verdict) is written with it, in the same write, so the
        record never holds the proposal without its review or the reverse."""
        ...

    def deliver_replan(self, run_id: str, replan: PlanReplan) -> PlanDelivery: ...

    def failed(self, run_id: str, reason: str) -> None: ...
