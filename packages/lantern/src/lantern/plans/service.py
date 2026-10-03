"""The rules every change to a plan goes through.

Every surface (the API today; the planner and chat later) creates and edits
plans here, so the rules hold once: a node breaks down one level at a time
(initiative → epic → task), a task lives in its epic's repository, a
dependency names a sibling task and never makes a cycle, a repository
whose forge cannot hold a plan is refused by name, and every mutation
names the revision it read. After publish the forge wins:
:meth:`PlanService.reconcile` folds it in (#2342), and the only writes to a
published node are a person's direct ones — :meth:`PlanService.edit_published`,
:meth:`~PlanService.attach` and :meth:`~PlanService.detach` (#2350) — and a
re-plan's diff a person approves entry by entry (#2346), each written to
the forge at once and never over a forge change.
"""

from __future__ import annotations

from lantern.plans.service_base import (
    IDLE,
    NO_FORGE,
    PLANNER,
    SECTIONS,
    TASK_SECTIONS,
    Attached,
    PlanRefusal,
    _changeable,
    _checked_answers,
    _current,
    _deleted,
    _kept,
    _node_changed,
    _not_found,
    _noun,
    _occupied,
    _stale,
    _stays,
    _title,
    _waiting,
    replanned,
)
from lantern.plans.service_drafts import _Drafting
from lantern.plans.service_forge import _ForgeWrites
from lantern.plans.service_planner import _Planning
from lantern.plans.service_questions import _Questions

__all__ = [
    "IDLE",
    "NO_FORGE",
    "PLANNER",
    "SECTIONS",
    "TASK_SECTIONS",
    "Attached",
    "PlanRefusal",
    "PlanService",
    "_changeable",
    "_checked_answers",
    "_current",
    "_deleted",
    "_kept",
    "_node_changed",
    "_not_found",
    "_noun",
    "_occupied",
    "_stale",
    "_stays",
    "_title",
    "_waiting",
    "replanned",
]


class PlanService(_Drafting, _Planning, _Questions, _ForgeWrites):
    """The one object every surface holds. Its parts — a person's drafting,
    the planner's side, the clarifying questions and the forge writes — are
    the mixins; what they share is :class:`~lantern.plans.service_base._ServiceBase`."""
