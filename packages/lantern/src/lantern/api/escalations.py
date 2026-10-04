"""What an agent asked a person to decide, as the attention list shows it.

The plan driver and triage write an ``escalate`` row to the
decisions ledger whenever no grant covers a step an agent wanted to take.
Each unresolved one is an ``escalation`` entry of ``GET /v1/attention``;
this module is everything about them that is not the list's own plumbing:

* :data:`ESCALATED` — the one map from a delegable action to the words a
  person reads (*"planner asks to publish the level under …"*), the
  capability a person needs to take that act themselves, and whether the
  server has a human path for it (``approve``). An action this build does
  not know is shown, offers only ``decline``, and needs ``policy:manage``:
  fail closed.
* :func:`gone` — whether what the escalation is about no longer exists
  (its plan, node or item). Such an escalation is left off the list as
  soon as the list is read, and resolved ``superseded`` by :func:`settle`
  on the attention tracker's next pass — the list itself never writes.
  Whether the step already happened is *not* judged here: the pass that
  escalated it owns that (``PlanDriver._resolve`` for the plan steps,
  ``daemon/triage.py`` for the retries and round grants). Here an
  escalation is resolved only by a person's ``approve`` or ``decline``,
  or by its target being gone.

**What ``approve`` does**, per action: the person takes the step the agent
proposed, through the same command the step's own route runs, recorded as
that route's operation under the person — then the decision is resolved
``acted`` by them.

====================  ==========================================  ===============
action                ``approve`` runs                            capability
====================  ==========================================  ===============
``plan.breakdown``    ``POST …/nodes/{node}/breakdown``           ``plans:create``
``plan.approve``      ``POST …/nodes/{node}/approve`` (all)       ``plans:create``
``plan.publish``      ``POST …/nodes/{node}/publish``             ``plans:publish``
``plan.run``          ``POST …/nodes/{node}/run``                 ``plans:publish``
``plan.run.retry``    ``POST …/nodes/{task}/run/retry``           ``plans:publish``
``item.retry``        ``POST /v1/items/{id}/retry``               ``runs:control``
``run.grant_rounds``  ``POST /v1/runs/{id}/grant_rounds``         ``budgets:grant``
``plan.propose``      — (no human path: ``decline`` only)         ``policy:manage``
====================  ==========================================  ===============

``decline`` is offered on every escalation, under the same capability:
the decision is resolved ``declined`` with the person as ``resolved_by``,
and nothing else changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lantern.daemon.controls.delegation_store import DecisionRecord, DelegationStore
from lantern.daemon.model import WorkItem
from lantern.plans.model import Plan
from lantern.plans.store import PlanStore

if TYPE_CHECKING:
    from lantern.daemon.controls.principal import Capability

#: The entry kind an unresolved escalation is.
KIND = "escalation"
#: What a person may do about one: take the step themselves, or say no.
APPROVE = "approve"
DECLINE = "decline"
#: The operation a ``decline`` records.
DECLINE_OPERATION = "decision.decline"


@dataclass(frozen=True, slots=True)
class Escalated:
    """How an escalated action reads, who may settle it, and whether a
    person can take it here. ``phrase`` takes ``{target}``."""

    phrase: str
    capability: Capability
    approvable: bool = True


#: The one action→phrase map: every place that names an escalated action
#: for a person reads it from here.
ESCALATED: dict[str, Escalated] = {
    "plan.propose": Escalated("propose a plan for {target}", "policy:manage", approvable=False),
    "plan.breakdown": Escalated("break {target} down into its next level", "plans:create"),
    "plan.approve": Escalated("approve the level proposed under {target}", "plans:create"),
    "plan.publish": Escalated("publish the level under {target} to the forge", "plans:publish"),
    "plan.run": Escalated("start running the tasks of {target}", "plans:publish"),
    "plan.run.retry": Escalated("retry the failed task {target}", "plans:publish"),
    "item.retry": Escalated("retry {target}", "runs:control"),
    "run.grant_rounds": Escalated("grant {target} more rounds", "budgets:grant"),
}
#: An action this build does not know: shown, never taken, owner's to settle.
_UNKNOWN = Escalated("take {action} on {target}", "policy:manage", approvable=False)


def escalated(action: str) -> Escalated:
    return ESCALATED.get(action, _UNKNOWN)


def capability(action: str) -> Capability:
    """What a person needs to approve or decline an escalated ``action``."""
    return escalated(action).capability


def actions(action: str) -> tuple[str, ...]:
    """What an escalation of ``action`` offers, in order."""
    return (APPROVE, DECLINE) if escalated(action).approvable else (DECLINE,)


def entry_id(decision_id: str) -> str:
    return f"{KIND}:{decision_id}"


def decision_id_of(entry_id_: str) -> str:
    return entry_id_.partition(":")[2]


def title(record: DecisionRecord, target: str | None) -> str:
    """``<agent> asks to <what>``, in plain words."""
    named = f"“{target}”" if target else (record.repository or "its target")
    what = escalated(record.action).phrase.format(target=named, action=record.action)
    return f"{record.agent_slug} asks to {what}"


@dataclass(frozen=True, slots=True)
class Escalation:
    """One unresolved escalation with what it is about, as the list read it."""

    record: DecisionRecord
    plan: Plan | None = None
    item: WorkItem | None = None

    @property
    def target(self) -> str | None:
        """What the escalation is about, as a person names it."""
        if self.plan is not None and self.record.node_id:
            node = self.plan.node(self.record.node_id)
            if node is not None:
                return node.title
        if self.item is not None:
            return self.item.title
        return None

    @property
    def repository(self) -> str | None:
        if self.record.repository:
            return self.record.repository
        if self.plan is not None and self.record.node_id:
            node = self.plan.node(self.record.node_id)
            if node is not None:
                return node.repository
        return None

    @property
    def revision(self) -> int | None:
        """The revision an act on it is checked against: the plan's, or
        the item's."""
        if self.plan is not None:
            return self.plan.revision
        if self.item is not None:
            return self.item.revision
        return None


def unresolved(dstore: Any) -> list[Escalation]:
    """Every escalation still waiting on a person, with its plan and its
    item read in one statement each; the ones whose target is gone are
    left out (:func:`gone`)."""
    return [e for e in _read(dstore) if not gone(dstore, e)]


def _read(dstore: Any) -> list[Escalation]:
    records: list[DecisionRecord] = []
    store = DelegationStore(dstore)
    after: tuple[float, str] | None = None
    while True:
        page = store.page(unresolved=True, after=after, limit=200)
        records.extend(page)
        if len(page) < 200:
            break
        after = (page[-1].at, page[-1].id)
    if not records:
        return []
    plans = PlanStore(dstore).many(r.plan_id for r in records if r.plan_id)
    wanted = list(dict.fromkeys(r.item_id for r in records if r.item_id))
    items: Mapping[str, WorkItem] = dstore.get_many(wanted) if wanted else {}
    return [
        Escalation(
            record,
            plan=plans.get(record.plan_id) if record.plan_id else None,
            item=items.get(record.item_id) if record.item_id else None,
        )
        for record in records
    ]


def gone(dstore: Any, escalation: Escalation) -> bool:
    """Whether what ``escalation`` is about no longer exists: its plan is
    gone or archived, its node removed, or its item gone or deleted. Only
    that is settled here. Whether the step has *happened* — or the
    situation that asked for it moved on — is for the pass that escalated
    it (the plan driver for the plan steps, triage for the retries and
    round grants), which knows its own situation; an escalation whose
    target still stands waits for them or for a person."""
    record, plan = escalation.record, escalation.plan
    if record.plan_id:
        if plan is None or plan.archived:
            return True
        if record.node_id and plan.node(record.node_id) is None:
            return True
    if record.item_id:
        item = escalation.item
        if item is None:
            return True
        return dstore.work_mark("item", item.item_id, "deleted") is not None
    return False


def settle(dstore: Any, now: float) -> list[DecisionRecord]:
    """Resolve ``superseded`` every escalation whose target is gone
    (:func:`gone`): the attention tracker's pass calls this, so the list
    read stays a read."""
    store = DelegationStore(dstore)
    settled: list[DecisionRecord] = []
    for escalation in _read(dstore):
        if gone(dstore, escalation):
            done = store.resolve(escalation.record.id, by=None, resolution="superseded", now=now)
            if done is not None:
                settled.append(done)
    return settled
