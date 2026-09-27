"""Planning work into the forge: initiatives, epics and tasks (#2334).

A **plan** is a tree of **nodes** kept in the daemon's store. A person
drafts it (``plans:create``), the planner proposes one level at a time, a
person approves and publishes a level to the forge (``plans:publish``), and
after publish the forge is the record. Every plan, drafts included, is
visible to anyone in the workspace who may read runs.

:mod:`~sbxloop.plans.model` holds the shapes, :mod:`~sbxloop.plans.store`
the rows, :mod:`~sbxloop.plans.hierarchy` what each forge can hold, and
:mod:`~sbxloop.plans.service` the rules every surface goes through.
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
