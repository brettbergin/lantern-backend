"""Planning work into the forge: initiatives, epics and tasks (#2334).

A **plan** is a tree of **nodes** kept in the daemon's store. A person
drafts it (``plans:create``), the planner proposes one level at a time, a
person approves and publishes a level to the forge (``plans:publish``), and
after publish the forge is the record. Every plan, drafts included, is
visible to anyone in the workspace who may read runs.

:mod:`~lantern.plans.model` holds the shapes, :mod:`~lantern.plans.store`
the rows, :mod:`~lantern.plans.hierarchy` what each forge can hold,
:mod:`~lantern.plans.render` a node as its issue body (and back),
:mod:`~lantern.plans.publish` the walk that writes a level to the forge,
:mod:`~lantern.plans.reconcile` the reading that folds the forge back in
after publish, :mod:`~lantern.plans.direct` a person's edits, attaches and
detaches written to the forge after publish, :mod:`~lantern.plans.replan` a
re-plan's approved diff applied through the publish path and the direct
edit's write, :mod:`~lantern.plans.complete` the summary and close of a
finished epic or initiative, :mod:`~lantern.plans.forgeread` what every
module that reads the forge shares, and :mod:`~lantern.plans.service` the
rules every surface goes through — one object, :class:`PlanService`, made
of four parts over a common base: a person's drafting
(:mod:`~lantern.plans.service_drafts`), the planner's side
(:mod:`~lantern.plans.service_planner`), the clarifying questions
(:mod:`~lantern.plans.service_questions`) and the forge writes
(:mod:`~lantern.plans.service_forge`), with the store, the refusal, the
busy sets and the checks they share in :mod:`~lantern.plans.service_base`.
"""

from __future__ import annotations

from lantern.plans.hierarchy import RepositoryPlanning, planning_available, repository_planning
from lantern.plans.model import LEVELS, Plan, PlanNode, child_level
from lantern.plans.service import PlanRefusal, PlanService

__all__ = [
    "LEVELS",
    "Plan",
    "PlanNode",
    "PlanRefusal",
    "PlanService",
    "RepositoryPlanning",
    "child_level",
    "planning_available",
    "repository_planning",
]
