"""Planning work into the forge: initiatives, epics and tasks (#2334).

A **plan** is a tree of **nodes** kept in the daemon's store. A person
drafts it (``plans:create``), the planner proposes one level at a time, a
person approves and publishes a level to the forge (``plans:publish``), and
after publish the forge is the record. Every plan, drafts included, is
visible to anyone in the workspace who may read runs.

:mod:`~sbxloop.plans.model` holds the shapes, :mod:`~sbxloop.plans.store`
the rows, :mod:`~sbxloop.plans.hierarchy` what each forge can hold,
:mod:`~sbxloop.plans.render` a node as its issue body (and back),
:mod:`~sbxloop.plans.publish` the walk that writes a level to the forge,
:mod:`~sbxloop.plans.reconcile` the reading that folds the forge back in
after publish, :mod:`~sbxloop.plans.direct` a person's edits, attaches and
detaches written to the forge after publish, :mod:`~sbxloop.plans.replan` a
re-plan's approved diff applied through the publish path and the direct
edit's write, :mod:`~sbxloop.plans.complete` the summary and close of a
finished epic or initiative, and :mod:`~sbxloop.plans.service` the rules
every surface goes through.
"""

from __future__ import annotations

from sbxloop.plans.hierarchy import RepositoryPlanning, planning_available, repository_planning
from sbxloop.plans.model import LEVELS, Plan, PlanNode, child_level
from sbxloop.plans.service import PlanRefusal, PlanService

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
