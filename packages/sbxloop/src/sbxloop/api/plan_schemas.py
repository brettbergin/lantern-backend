"""The request and response shapes of ``/v1/plans``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sbxloop.api.models import ApiModel, Item, OperationOut
from sbxloop.daemon.controls.principal import WORKSPACE_ID


class PlanForge(ApiModel):
    """The issue a published node became, as the forge last said.
    ``version`` is the version of the issue the node holds — a digest of its
    title and sections as sbxloop last read or wrote them — which an edit of
    the node names as ``forge_version``. ``updated_at`` is the forge's own
    timestamp as last read (informational: a comment or a label moves it).
    ``detached`` says why the node no longer follows its issue (it left its
    parent or the forge, or a person detached it); ``marker_missing`` that
    its body lost the ``sbx-plan`` marker; ``checklist_error`` why its
    managed children checklist could not be read. None of these is
    repaired by sbxloop."""

    number: int
    url: str
    state: Literal["open", "closed"] | None = None
    version: str | None = None
    updated_at: str | None = None
    detached: str | None = None
    marker_missing: bool = False
    checklist_error: str | None = None


class PlanDrift(ApiModel):
    """One change the forge made to a node that nobody has marked seen.
    ``title``, ``sections`` and ``state`` carry ``before`` (as a person
    last saw it) and ``after`` (as the forge has it now), keyed by field,
    for a diff; ``adopted``, ``moved`` and ``reattached`` carry the parent
    in ``after`` (and ``before``); ``detached``, ``marker_removed`` and
    ``checklist_mangled`` say why in ``reason``."""

    change: Literal[
        "title",
        "sections",
        "state",
        "adopted",
        "moved",
        "detached",
        "reattached",
        "marker_removed",
        "checklist_mangled",
    ]
    at: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None


class PlanChoiceOut(ApiModel):
    """One answer a person may pick: ``value`` is what is recorded,
    ``label`` what the button says."""

    value: str
    label: str
    description: str | None = None


class PlanQuestionOut(ApiModel):
    """A clarifying question in the chat choice question's shape: two to
    five choices, and free text unless ``allow_free_text`` is false."""

    id: str
    prompt: str
    choices: list[PlanChoiceOut]
    allow_free_text: bool = True


class PlanAnswerOut(ApiModel):
    """A person's answer to one question: the ``value`` of the choice
    picked, or ``text`` in their own words."""

    value: str | None = None
    text: str = ""


class PlanGenerationOut(ApiModel):
    """The node's latest clarifying questions (#2345): the run that asked,
    what it asked, and where they stand — ``awaiting_answers`` while the
    run waits (a client shows the questions card), then ``answered``,
    ``skipped``, or ``withdrawn`` when the run was given up first.
    ``answers`` is keyed by question id."""

    run_id: str
    status: Literal["awaiting_answers", "answered", "skipped", "withdrawn"]
    questions: list[PlanQuestionOut]
    answers: dict[str, PlanAnswerOut] = Field(default_factory=dict)
    asked_at: str
    answered_at: str | None = None
    answered_by: str | None = None


class PlanReplanEntryOut(ApiModel):
    """One entry of a re-plan's diff, waiting for a person.

    ``add``: ``node_id`` is the id the new child will have and ``sections``
    the whole child (``depends_on`` naming node ids). ``modify``:
    ``node_id`` is the child changed, ``sections`` only what changes and
    ``before`` those sections as the child had them when the diff was
    proposed. ``suggest_close``: ``node_id`` is the child the planner would
    close. ``forge_version`` (``modify``, ``suggest_close``) is the child's
    issue version when the diff was proposed: a change is written only
    while the issue still reads so. ``error`` is why the last approval of
    this entry failed."""

    id: str
    action: Literal["add", "modify", "suggest_close"]
    node_id: str
    sections: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    error: str | None = None
    forge_version: str | None = None


class PlanReplanOut(ApiModel):
    """A re-plan's diff waiting on a published node: the ``plan`` run that
    proposed it, when, and the entries not yet approved or discarded."""

    id: str
    run_id: str | None = None
    proposed_at: str
    entries: list[PlanReplanEntryOut]


class PlanNodeOut(ApiModel):
    id: str
    parent_id: str | None = None
    position: int
    level: Literal["initiative", "epic", "task"]
    repository: str
    state: Literal["draft", "proposed", "approved", "published"]
    origin: Literal["person", "planner", "forge"]
    title: str
    goal: str = ""
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    kind: Literal["code", "workload"] | None = None
    workload_profile: str | None = None
    verify_commands: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    non_goals: str = ""
    constraints: str = ""
    forge: PlanForge | None = None
    #: The node's latest clarifying questions and their answers; null until
    #: a breakdown of the node asks something.
    generation: PlanGenerationOut | None = None
    created_at: str
    updated_at: str
    #: What the forge changed that nobody has marked seen, oldest first.
    drift: list[PlanDrift] = Field(default_factory=list)
    #: A re-plan's diff waiting for a person, or null.
    replan: PlanReplanOut | None = None


class PlanRollup(ApiModel):
    """How far the plan has got: its epics and tasks, how many tasks the
    forge has closed, and how many nodes are published."""

    epics: int = 0
    tasks: int = 0
    tasks_closed: int = 0
    published: int = 0


class PlanSummary(ApiModel):
    id: str
    workspace_id: str = WORKSPACE_ID
    title: str
    level: Literal["initiative", "epic"]
    repository: str
    state: Literal["draft", "published", "archived"]
    revision: int
    root_id: str
    created_by: str | None = None
    created_by_display: str | None = None
    created_at: str
    updated_at: str
    rollup: PlanRollup
    #: How many nodes carry drift nobody has marked seen: the badge.
    drift: int = 0
    #: When the forge was last read into the plan, and what stopped the
    #: last reading (or part of it); a plan served while the forge is down
    #: says so here.
    reconciled_at: str | None = None
    reconcile_error: str | None = None


class PlanInput(ApiModel):
    """A person's requirements for inference, separate from generated issue content."""

    title: str
    goal: str = ""
    context: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    non_goals: str = ""
    constraints: str = ""


class PlanOut(PlanSummary):
    """The whole tree: the root first, then each parent's children in
    order."""

    nodes: list[PlanNodeOut]
    input: PlanInput | None = None
    generation_pending: bool = False


class PlanSections(ApiModel):
    """A node's sections. A key left out keeps its value."""

    title: str | None = Field(default=None, min_length=1, max_length=256)
    goal: str | None = Field(default=None, max_length=8000)
    context: str | None = Field(default=None, max_length=16000)
    acceptance_criteria: list[str] | None = Field(default=None, max_length=50)
    non_goals: str | None = Field(default=None, max_length=4000)
    constraints: str | None = Field(default=None, max_length=4000)


class TaskSections(PlanSections):
    """A task's sections on top of every node's."""

    kind: Literal["code", "workload"] | None = None
    workload_profile: str | None = Field(default=None, max_length=120)
    verify_commands: list[str] | None = Field(default=None, max_length=30)
    depends_on: list[str] | None = Field(default=None, max_length=50)


class PlanCreate(PlanSections):
    """A planning brief for inference: an initiative or a lone epic.
    Sections are stored in the plan's input, never as root issue content.
    A breakdown generates the parent and its immediate children."""

    level: Literal["initiative", "epic"]
    repository: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=1, max_length=256)


class PlanUpdate(PlanSections):
    """Edit the inference brief while generation_pending, otherwise the
    generated root's sections."""

    expected_revision: int = Field(ge=1)


class PlanNodeCreate(TaskSections):
    """A new child one level under ``parent_id``. ``repository`` is for an
    epic under an initiative (the initiative's home by default); a task
    always lives in its epic's repository. ``position`` places it among its
    siblings (last by default)."""

    expected_revision: int = Field(ge=1)
    parent_id: str = Field(min_length=1, max_length=64)
    repository: str | None = Field(default=None, min_length=3, max_length=200)
    position: int | None = Field(default=None, ge=0)
    title: str = Field(min_length=1, max_length=256)


class PlanNodeUpdate(TaskSections):
    """Edit a node's sections, or move it among its siblings. An edit of a
    published node's sections writes its issue (``plans:publish``) and
    names ``forge_version``: the version of the issue the client read — the
    node's ``forge.version``, or the one a ``409 forge_changed`` answered
    with."""

    expected_revision: int = Field(ge=1)
    position: int | None = Field(default=None, ge=0)
    forge_version: str | None = Field(default=None, min_length=1, max_length=128)


class PlanAttach(ApiModel):
    """Attach an existing open issue as a child of the node: its
    ``repository`` and ``number``, or its web ``url``."""

    expected_revision: int = Field(ge=1)
    repository: str | None = Field(default=None, min_length=3, max_length=200)
    number: int | None = Field(default=None, ge=1)
    url: str | None = Field(default=None, min_length=8, max_length=500)


class PlanAttached(ApiModel):
    """The plan as it now is and the node that follows the attached issue.
    ``linked`` is how it sits under its parent: a ``native`` sub-issue or a
    ``checklist`` line; ``reason`` says why a native link became a
    checklist line."""

    plan: PlanOut
    node_id: str
    linked: Literal["native", "checklist"]
    reason: str | None = None


class PlanDetach(ApiModel):
    """Unlink a published child from its parent; its issue stays open."""

    expected_revision: int = Field(ge=1)


class PlanDeleted(ApiModel):
    id: str
    outcome: Literal["deleted", "archived"]


class PlanApprove(ApiModel):
    """Approve a node's draft and proposed children: every one, or the ones
    ``node_ids`` names."""

    expected_revision: int = Field(ge=1)
    node_ids: list[str] | None = Field(default=None, max_length=100)


class PlanDriftAck(ApiModel):
    """Mark the forge's changes seen: on every node, or the nodes
    ``node_ids`` names."""

    expected_revision: int = Field(ge=1)
    node_ids: list[str] | None = Field(default=None, max_length=500)


class PlanPublish(ApiModel):
    """Publish a node's level. The ``Idempotency-Key`` header is required."""

    expected_revision: int = Field(ge=1)


class PlanPublishResult(ApiModel):
    """What publishing did with one node: ``created`` its issue, ``found``
    the one an earlier attempt created (by its marker), or ``failed`` with
    the forge's words in ``error`` (the node stays as it was, and repeating
    the publish resumes it). ``linked`` is how it sits under its parent;
    ``reason`` says why a native link became a checklist line."""

    node_id: str
    outcome: Literal["created", "found", "failed"]
    number: int | None = None
    url: str | None = None
    linked: Literal["native", "checklist", "none"] = "none"
    error: str | None = None
    reason: str | None = None


class PlanPublished(ApiModel):
    """The plan as it now is, and what happened to each node published.
    ``replayed`` is true when the answer is a replay of an earlier call
    under the same ``Idempotency-Key`` (its results, the plan as it is
    now)."""

    plan: PlanOut
    results: list[PlanPublishResult]
    operation_id: str | None = None
    replayed: bool = False


class PlanBreakdown(ApiModel):
    """Start a breakdown: a ``plan`` run proposing the node's next level."""

    expected_revision: int = Field(ge=1)
    #: What the person wants the planner to keep in mind for this level.
    note: str | None = Field(default=None, max_length=8000)
    #: The channel the run answers to: its chronology is told there and a
    #: person there can steer it.
    channel_id: str | None = Field(default=None, max_length=128)


class PlanBreakdownAccepted(ApiModel):
    """The breakdown was queued as a ``plan`` run: the work item (its
    ``run_id`` once dispatched) and the operation that admitted it. The
    proposal arrives on the plan as ``plan.generation.proposed``."""

    plan_id: str
    node_id: str
    item: Item
    operation: OperationOut
    #: ``False`` when the same request had already queued it.
    created: bool


class PlanAnswerIn(ApiModel):
    """An answer to one question: a choice's ``value``, or ``text`` in the
    person's own words where the question allows it (both is a choice with
    a remark)."""

    value: str | None = Field(default=None, min_length=1, max_length=200)
    text: str | None = Field(default=None, max_length=4000)


class PlanAnswers(ApiModel):
    """Answer the questions a breakdown of the node is waiting on, keyed by
    question id, or ``skip`` them to let the planner decide; either
    resumes the run. A question left out goes to the planner unanswered."""

    #: The plan revision the answers were read against; a stale one is
    #: ``409 stale_revision``. Optional: the questions are what is answered.
    expected_revision: int | None = Field(default=None, ge=1)
    answers: dict[str, PlanAnswerIn] = Field(default_factory=dict, max_length=10)
    skip: bool = False


class PlanAnswersAccepted(ApiModel):
    """The answers were recorded: the plan as it now is, and whether the
    run waiting on them went back to the queue (it does once they are
    settled, which an answer through this route always does)."""

    plan: PlanOut
    run_id: str
    resumed: bool


class PlanReplanApprove(ApiModel):
    """Apply a waiting re-plan: every entry, or the ones ``entry_ids``
    names. The ``Idempotency-Key`` header is required."""

    expected_revision: int = Field(ge=1)
    entry_ids: list[str] | None = Field(default=None, max_length=100)


class PlanReplanDiscard(ApiModel):
    """Drop a waiting re-plan: every entry, or the ones ``entry_ids``
    names. Nothing is written to the forge."""

    expected_revision: int = Field(ge=1)
    entry_ids: list[str] | None = Field(default=None, max_length=100)


class PlanReplanResult(ApiModel):
    """What approving one entry did: an addition ``created`` (or ``found``,
    the issue an interrupted attempt filed, by its marker), a child
    ``updated`` or ``closed``, or ``failed`` with why in ``error`` — the
    entry stays in the diff. ``reason`` notes what went wrong beside a
    success (a checklist fallback, a comment that could not be posted)."""

    entry_id: str
    action: Literal["add", "modify", "suggest_close"]
    outcome: Literal["created", "found", "updated", "closed", "failed"]
    node_id: str
    number: int | None = None
    url: str | None = None
    error: str | None = None
    reason: str | None = None


class PlanReplanApplied(ApiModel):
    """The plan as it now is, and what happened to each entry approved.
    ``replayed`` is true for a replay under the same ``Idempotency-Key``."""

    plan: PlanOut
    results: list[PlanReplanResult]
    operation_id: str | None = None
    replayed: bool = False


class EpicRunStart(ApiModel):
    """Run a published epic. The ``Idempotency-Key`` header is required."""

    expected_revision: int = Field(ge=1)


class EpicRunTaskOut(ApiModel):
    """One task of an epic run. ``state`` is ``waiting`` (a dependency is
    not closed yet), ``ready`` (about to be admitted, or the forge could
    not be read), ``queued``, ``running``, ``landed`` (its code run merged
    or its workload delivered, and its issue was closed), ``closed`` (its
    issue was already closed), ``failed`` (``reason`` says why; retry or
    skip it), ``blocked`` (a dependency failed or is blocked, so it is not
    admitted; ``reason`` names which), ``skipped`` (a person treated it as
    done; its issue is left as it is) or ``cancelled`` (the run was stopped
    before it was admitted, or while its item was still queued). ``item_id``
    is the item it was admitted as and ``run_id`` the run that item last
    started — the run's thread."""

    node_id: str
    title: str
    kind: Literal["code", "workload"] | None = None
    workload_profile: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    forge: PlanForge | None = None
    state: Literal[
        "waiting",
        "ready",
        "queued",
        "running",
        "landed",
        "closed",
        "failed",
        "blocked",
        "skipped",
        "cancelled",
    ]
    item_id: str | None = None
    run_id: str | None = None
    reason: str | None = None
    admitted_at: str | None = None
    updated_at: str


class EpicRunOut(ApiModel):
    """An epic run: the daemon admitting an epic's ready tasks as issue
    runs, in dependency order, with ``parent_item_id`` naming it. ``state``
    is ``running`` (admitting), ``paused`` (a person paused it: what is
    queued or running goes on, nothing new is admitted), ``completed``
    (every task landed, closed or skipped) or ``cancelled`` (stopped:
    nothing more is admitted; a run already under way finishes)."""

    id: str
    plan_id: str
    node_id: str
    state: Literal["running", "paused", "completed", "cancelled"]
    started_by: str | None = None
    started_by_display: str | None = None
    created_at: str
    updated_at: str
    completed_at: str | None = None
    tasks: list[EpicRunTaskOut]


class EpicRunStarted(EpicRunOut):
    """The epic run as it stands after its first pass. ``replayed`` is true
    when the answer is a replay of an earlier call under the same
    ``Idempotency-Key`` (the run as it is now)."""

    operation_id: str | None = None
    replayed: bool = False


class EpicRunChanged(EpicRunOut):
    """The epic run after a control — pause, resume, cancel, or a task's
    retry or skip — and the pass it made. ``replayed`` is true when the
    answer is a replay of an earlier call under the same
    ``Idempotency-Key`` (the run as it is now)."""

    operation_id: str | None = None
    replayed: bool = False
