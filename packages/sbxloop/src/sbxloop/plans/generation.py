"""The plan record a ``plan`` run reads from and delivers to, in the daemon.

The engine knows a plan only as a :class:`~sbxloop.engine.planning.PlanDesk`;
this is the daemon's, one per plan work item: the brief is read from the
plan service as the plan is when the run proposes, the proposal is written
through the service's rules, and the generation's start and failure are
recorded as ``plan.generation.*`` events scoped to the run, its item and its
channel. Nothing here reaches the forge.
"""

from __future__ import annotations

from collections.abc import Callable

from sbxloop.daemon.model import WorkItem
from sbxloop.engine.planning import PlanBrief, PlanDelivery, PlanProposal
from sbxloop.errors import PlanDeliveryError
from sbxloop.log import get_logger
from sbxloop.plans.service import PlanRefusal, PlanService

log = get_logger(__name__)


class PlanGeneration:
    """One plan item's node, as its run's desk."""

    def __init__(self, service: PlanService, item: WorkItem, clock: Callable[[], float]) -> None:
        if item.plan_id is None or item.plan_node_id is None:
            raise ValueError(f"{item.item_id} names no plan node")
        self.service = service
        self.item = item
        self.plan_id = item.plan_id
        self.node_id = item.plan_node_id
        self.clock = clock

    def brief(self) -> PlanBrief:
        try:
            return self.service.brief(self.plan_id, self.node_id, note=self.item.body)
        except PlanRefusal as exc:
            raise PlanDeliveryError(exc.detail) from exc

    def started(self, run_id: str) -> None:
        self._notice("plan.generation.started", run_id)

    def deliver(self, run_id: str, proposal: PlanProposal) -> PlanDelivery:
        try:
            plan, count = self.service.deliver_proposal(
                self.plan_id,
                self.node_id,
                proposal,
                run_id=run_id,
                now=self.clock(),
                item_id=self.item.item_id,
                channel_id=self.item.channel_id,
            )
        except PlanRefusal as exc:
            raise PlanDeliveryError(exc.detail) from exc
        return PlanDelivery(count, f"plan {plan.id}, node {self.node_id}")

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
