"""The request and response shapes of ``/v1/plans``."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from sbxloop.api.models import ApiModel
from sbxloop.daemon.controls.principal import WORKSPACE_ID


class PlanForge(ApiModel):
    """The issue a published node became, as the forge last said."""

    number: int
    url: str
    state: Literal["open", "closed"] | None = None


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
    """Edit a node's sections, or move it among its siblings."""

    expected_revision: int = Field(ge=1)
    position: int | None = Field(default=None, ge=0)


class PlanDeleted(ApiModel):
    id: str
    outcome: Literal["deleted", "archived"]
