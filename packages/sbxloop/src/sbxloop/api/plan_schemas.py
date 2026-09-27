"""The request and response shapes of ``/v1/plans``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from sbxloop.api.models import ApiModel
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
    created_at: str
    updated_at: str
    #: What the forge changed that nobody has marked seen, oldest first.
    drift: list[PlanDrift] = Field(default_factory=list)


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


class PlanOut(PlanSummary):
    """The whole tree: the root first, then each parent's children in
    order."""

    nodes: list[PlanNodeOut]


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
    """A new draft plan from the form: an initiative (its home repository)
    or a lone epic (the repository it targets)."""

    level: Literal["initiative", "epic"]
    repository: str = Field(min_length=3, max_length=200)
    title: str = Field(min_length=1, max_length=256)


class PlanUpdate(PlanSections):
    """Edit the plan's own sections (its root node's)."""

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
