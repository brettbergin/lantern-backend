"""The plan record a ``plan`` run reads from and delivers to, in the daemon.

The engine knows a plan only as a :class:`~lantern.engine.planning.PlanDesk`;
this is the daemon's, one per plan work item: the brief is read from the
plan service as the plan is when the run proposes, the planner's
clarifying questions and its proposal (with the critic's verdict on it,
for a plan that advances itself) are written through the service's
rules, and the generation's start and failure are
recorded as ``plan.generation.*`` events scoped to the run, its item and its
channel. Nothing here writes to the forge: a re-plan's brief (#2346) is
read after the plan is reconciled from it — on the host, reading only — so
the planner sees the children as the forge has them now, and a forge that
cannot be read fails the run named rather than re-plan against a stale tree.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from lantern.agents.assignment import AgentAssignment
from lantern.daemon.model import WorkItem, is_planned_assignment
from lantern.engine.planning import (
    PlanBrief,
    PlanDelivery,
    PlanProposal,
    PlanQuestion,
    PlanReplan,
    PlanVerdict,
)
from lantern.errors import PlanDeliveryError
from lantern.log import get_logger
from lantern.plans.service import PLANNER, PlanRefusal, PlanService, replanned

log = get_logger(__name__)


def planner_of(item: WorkItem) -> str | None:
    """Who a plan item's run proposes as: ``agent:<slug>`` of the agent its
    assignment binds to the ``plan`` phase. Dispatch stores the assignment
    on the item before the desk is built, and every attempt reuses it, so
    this is the agent that actually takes the turn. ``None`` when the item
    carries no planned assignment (or one that cannot be read, or binds
    nobody to the phase): nobody is recorded rather than a guess."""
    return _bound(item, "plan")


def reviewer_of(item: WorkItem) -> str | None:
    """Who a plan item's run reviews its proposal as: ``agent:<slug>`` of
    the critic its assignment binds to the ``review`` phase, the phase the
    review turn runs as; ``None`` on the same terms as :func:`planner_of`."""
    return _bound(item, "review")


def _bound(item: WorkItem, phase: str) -> str | None:
    if not is_planned_assignment(item.assignment_json):
        return None
    assert item.assignment_json is not None  # nosec B101 - checked above
    try:
        binding = AgentAssignment.from_json(item.assignment_json).binding_for(phase)
    except (KeyError, TypeError, ValueError):
        return None
    return None if binding is None else f"agent:{binding.slug}"


class PlanGeneration:
    """One plan item's node, as its run's desk."""

    def __init__(
        self,
        service: PlanService,
        item: WorkItem,
        clock: Callable[[], float],
        *,
        forge: Any | None = None,
    ) -> None:
        if item.plan_id is None or item.plan_node_id is None:
            raise ValueError(f"{item.item_id} names no plan node")
        self.service = service
        self.item = item
        self.plan_id = item.plan_id
        self.node_id = item.plan_node_id
        self.clock = clock
        # Who the run's proposals are recorded as proposed by.
        self.planner = planner_of(item)
        # The daemon's forge connection, read (never written) before a
        # re-plan so its children are the forge's.
        self.forge = forge

    def brief(self, *, fresh: bool = False) -> PlanBrief:
        try:
            if fresh:
                self._reconcile()
            return self.service.brief(self.plan_id, self.node_id, note=self.item.body)
        except PlanRefusal as exc:
            raise PlanDeliveryError(exc.detail) from exc

    def _reconcile(self) -> None:
        """Read a re-planned node's tree from the forge into the plan first;
        a breakdown reads nothing."""
        plan, node = self.service.breakdown_target(self.plan_id, self.node_id)
        if not replanned(plan, node):
            return
        forge = self.forge

        def connect() -> Any:
            if forge is None:
                raise PlanDeliveryError("the daemon has no forge connection")
            return forge.call(lambda ops: ops)

        try:
            result = self.service.reconcile(
                self.plan_id,
                forge_kind=None if forge is None else str(forge.kind),
                connect=connect,
                clock=self.clock,
                actor=PLANNER,
                force=True,
            )
        except PlanRefusal as exc:
            if exc.status == 404:
                raise
            raise PlanDeliveryError(
                f"the forge could not be read before re-planning: {exc.detail}"
            ) from exc
        if result.error:
            raise PlanDeliveryError(
                f"the forge could not be read before re-planning: {result.error}"
            )

    def started(self, run_id: str) -> None:
        self._notice("plan.generation.started", run_id)

    def ask(self, run_id: str, questions: Sequence[PlanQuestion]) -> None:
        try:
            self.service.ask_questions(
                self.plan_id,
                self.node_id,
                questions,
                run_id=run_id,
                now=self.clock(),
                item_id=self.item.item_id,
                channel_id=self.item.channel_id,
            )
        except PlanRefusal as exc:
            raise PlanDeliveryError(exc.detail) from exc

    def deliver(
        self, run_id: str, proposal: PlanProposal, *, review: PlanVerdict | None = None
    ) -> PlanDelivery:
        try:
            plan, count = self.service.deliver_proposal(
                self.plan_id,
                self.node_id,
                proposal,
                run_id=run_id,
                now=self.clock(),
                item_id=self.item.item_id,
                channel_id=self.item.channel_id,
                proposed_by=self.planner,
                review=review,
                reviewed_by=None if review is None else reviewer_of(self.item),
            )
        except PlanRefusal as exc:
            raise PlanDeliveryError(exc.detail) from exc
        return PlanDelivery(count, f"plan {plan.id}, node {self.node_id}")

    def deliver_replan(self, run_id: str, replan: PlanReplan) -> PlanDelivery:
        try:
            plan, count = self.service.deliver_replan(
                self.plan_id,
                self.node_id,
                replan,
                run_id=run_id,
                now=self.clock(),
                item_id=self.item.item_id,
                channel_id=self.item.channel_id,
                proposed_by=self.planner,
            )
        except PlanRefusal as exc:
            raise PlanDeliveryError(exc.detail) from exc
        return PlanDelivery(count, f"plan {plan.id}, node {self.node_id} (re-plan)")

    def failed(self, run_id: str, reason: str) -> None:
        self._notice("plan.generation.failed", run_id, reason=reason)

    def _notice(self, type_: str, run_id: str, **data: str) -> None:
        """A notice never fails the run it is about."""
        try:
            self.service.generation_event(
                type_,
                self.plan_id,
                self.node_id,
                run_id=run_id,
                now=self.clock(),
                item_id=self.item.item_id,
                channel_id=self.item.channel_id,
                **data,
            )
        except Exception:
            log.warning("plan.generation_notice_failed", notice=type_, run=run_id, exc_info=True)
