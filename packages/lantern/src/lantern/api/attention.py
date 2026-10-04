"""What is waiting on a person, as one list (``GET /v1/attention``).

Every client used to assemble this itself — items, gates, the queue, plans
and each channel's work, joined by rules of its own — so two clients could
disagree at the edges and nothing on the server could name "the thing
waiting on you". Here it is computed on read from what the daemon already
keeps; nothing is stored.

The read has two halves so its cost does not grow with what is waiting.
:func:`waiting` finds everything in a fixed number of statements (the open
gates, the items in the states that ask for someone, the live epic runs,
the provider hold, the polling health) and keeps only what ordering and
counting need. :func:`entries` then projects the one page a caller asked
for: its public ids, its runs, its marks and its conversations, each read
once for the page.

An entry's actions are the ones eligibility
(:mod:`lantern.daemon.controls.eligibility`) answers for the work as it
stands — the same answer the item, run and gate listings advertise — each
with the capability its route requires and whether this caller holds it.

Decisions are entries too (``attention.decisions``): every unresolved
escalation in the decisions ledger (:mod:`lantern.api.escalations`, whose
``approve`` and ``decline`` need the escalated step's capability), and on
a ``manual`` plan its breakdown's waiting questions (``plan_questions``,
only where no parked ``plan`` item already stands for them) and each
proposed level (``plan_proposal``). A plan that advances itself shows no
proposal: it reaches a person only through its escalations.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lantern.api import escalations
from lantern.api.admin import ADMIN_ACTIONS
from lantern.api.commands import ACTIONS as COMMANDS
from lantern.api.models import (
    AttentionAction,
    AttentionCounts,
    AttentionEntry,
    AttentionGroup,
    rfc3339,
)
from lantern.api.projections import Views, item_repository
from lantern.api.publicids import item_key, run_public_id
from lantern.daemon.controls.eligibility import Action
from lantern.daemon.controls.principal import Capability, Principal
from lantern.daemon.model import WorkItem
from lantern.daemon.store import REVIEW_WAIT_STATES, MergeGate
from lantern.plans.epicrun import LIVE_RUN_STATES, EpicRun, EpicRunStore, EpicRunTask, from_item
from lantern.plans.model import Plan, PlanNode
from lantern.plans.store import PlanStore
from lantern.provider import ProviderHold, ProviderRecovery

if TYPE_CHECKING:
    from lantern.api.auth.deps import Authenticated

#: The groups, in the order the list shows them: what needs a decision,
#: what ended needing someone, what is held until someone clears it.
GROUPS: tuple[AttentionGroup, ...] = ("decision", "failed", "paused")
_RANK: dict[str, int] = {group: rank for rank, group in enumerate(GROUPS)}

#: Item states parked on a person: a review to give, questions to answer.
#: A ``gated`` item is not here — its open gate is the entry.
PARKED_ITEM_STATES: tuple[str, ...] = ("awaiting_review", "paused_review", "awaiting_answers")
#: Item states that ended needing a person. ``cancelled`` is not one of
#: them: stopping work is a person's own act, and it rests there without
#: asking anything — a dismissal is accepted on it, and none is needed.
ENDED_ITEM_STATES: tuple[str, ...] = ("failed", "blocked")

#: A review hold a run is still parked on (``open``, ``paused``) or just
#: leaving (``approving``, ``fixing``): the ones a parked item's run has.
_STANDING_HOLD_STATES: tuple[str, ...] = ("open", "paused", "approving", "fixing")

#: The order an entry lists its actions in: what settles the wait first,
#: what gives the work up or puts the alert away last.
ACTION_ORDER: tuple[Action, ...] = (
    "gate_approve",
    "review_wait_resume",
    "grant_rounds",
    "resume",
    "retry",
    "requeue",
    "steer",
    "cancel",
    "abandon",
    "dismiss",
    "undismiss",
    "delete",
)
#: Each of those as the command a client sends for it: where its
#: capability is read from, so the two can never disagree, and the name
#: the operation it records carries.
WORK_COMMANDS: dict[str, str] = {
    "gate_approve": "gate.approve",
    "review_wait_resume": "run.review_resume",
    "grant_rounds": "run.grant_rounds",
    "resume": "run.resume",
    "retry": "item.retry",
    "requeue": "item.requeue",
    "steer": "run.steer",
    "cancel": "run.cancel",
    "abandon": "item.abandon",
    "dismiss": "item.dismiss",
    "undismiss": "item.undismiss",
    "delete": "item.delete",
}
#: What a person does about a failed task of an epic run, through the
#: run's own routes (``…/run/retry`` and ``…/run/skip``).
TASK_ACTIONS: tuple[str, ...] = ("task_retry", "task_skip")
#: What those two routes require (``api/routes/plan_runs.py``).
_TASK_CAPABILITY = "plans:publish"
REPOSITORY_ACTIONS: tuple[str, ...] = ("repository_resume",)

#: The kinds about a plan a person settles on the plan's own page: a
#: breakdown's questions (answered there) and a proposed level (approved
#: there, or here).
PLAN_QUESTIONS = "plan_questions"
PLAN_PROPOSAL = "plan_proposal"
#: Approving a proposed level: the plan's approve route, and what it needs.
PROPOSAL_ACTIONS: tuple[str, ...] = (escalations.APPROVE,)
PROPOSAL_CAPABILITY: Capability = "plans:create"
#: The kinds whose actions are decided per entry, not per action name: an
#: escalation's capability is the escalated act's.
DECISION_KINDS: tuple[str, ...] = (escalations.KIND, PLAN_PROPOSAL)


def gate_entry_id(gate_id: str) -> str:
    """The id of the entry an open gate is, by the gate's public id."""
    return f"gate:{gate_id}"


def capability_for(action: str) -> str:
    """The capability the route behind ``action`` requires."""
    if action in TASK_ACTIONS:
        return _TASK_CAPABILITY
    if action in REPOSITORY_ACTIONS:
        return ADMIN_ACTIONS["repository.resume"][0]
    return COMMANDS[WORK_COMMANDS[action]][0]


@dataclass(frozen=True, slots=True)
class Waiting:
    """One thing waiting on a person, as the first half of the read holds
    it: enough to filter, count and order, and the records the page's
    projection starts from."""

    kind: str
    group: AttentionGroup
    since: float | None
    #: The internal natural key: unique within the kind, and what breaks a
    #: tie and resumes a page. Never shown.
    key: str
    repo: str | None = None
    gate: MergeGate | None = None
    item: WorkItem | None = None
    epic_run: EpicRun | None = None
    task: EpicRunTask | None = None
    node: PlanNode | None = None
    hold: ProviderHold | None = None
    health: Mapping[str, Any] | None = None
    plan: Plan | None = None
    escalation: escalations.Escalation | None = None

    @property
    def order(self) -> tuple[int, float, str, str]:
        """Decisions, then failures, then pauses; the longest wait first."""
        return (_RANK[self.group], self.since or 0.0, self.kind, self.key)


def _items_as_stored(dstore: Any, item_ids: Iterable[str]) -> dict[str, WorkItem]:
    """The items for ids another row names, in one query; an id the table
    spells the other way is looked up alone."""
    wanted = list(dict.fromkeys(item_ids))
    found: dict[str, WorkItem] = dstore.get_many(wanted)
    for item_id in wanted:
        if item_id not in found:
            item = dstore.get(item_id)
            if item is not None:
                found[item_id] = item
    return found


def waiting(views: Views, *, include_dismissed: bool = False) -> list[Waiting]:
    """Everything waiting on a person right now, unordered. A dismissed
    alert is left out unless ``include_dismissed``; a deleted one always."""
    dstore = views.dstore
    found: list[Waiting] = []

    # Open gates. A gate being approved is the daemon's to finish, not a
    # person's; it is back here if the approval fails and reopens it.
    gates: list[MergeGate] = dstore.merge_gates(["open"])
    views.note_parked(gates=gates)
    gate_items = _items_as_stored(dstore, [gate.item_id for gate in gates])
    views.load_dismissals(
        item_ids=[item.item_id for item in gate_items.values()],
        run_ids=[
            gate.run_id
            for gate in gates
            if gate.item_id in gate_items and gate_items[gate.item_id].run_id != gate.run_id
        ],
    )
    gated: set[str] = set()
    for gate in gates:
        item = gate_items.get(gate.item_id)
        if item is not None:
            # The gated item and its gate are one waiting thing.
            gated.add(item.item_id)
            if views.deleted_at(item, gate.run_id) is not None:
                continue
            if not include_dismissed and views.dismissal(item, gate.run_id) is not None:
                continue
        repo = gate.repo or (item_repository(item) if item is not None else None)
        found.append(
            Waiting("gate", "decision", gate.created_at, gate.run_id, repo, gate=gate, item=item)
        )

    # Items parked on a person, and items that ended needing one.
    items: dict[str, WorkItem] = {
        item.item_id: item
        for item in dstore.attention_items(
            (*PARKED_ITEM_STATES, *ENDED_ITEM_STATES), include_dismissed=include_dismissed
        )
        if item.item_id not in gated
    }

    # Failed tasks of live epic runs. A task failure leaves its run
    # `running`, so the task is the entry, not the run; a task `blocked`
    # behind it waits on the same decision and is no entry of its own.
    failed: list[tuple[EpicRun, EpicRunTask, PlanNode | None]] = []
    plans = PlanStore(dstore)
    for run in EpicRunStore(dstore).active():
        if run.state not in LIVE_RUN_STATES:
            # Stopped, and only followed to its end: its tasks take no
            # retry and no skip. What failed is still an item entry.
            continue
        stuck = [task for task in run.tasks if task.state == "failed"]
        if not stuck:
            continue
        plan = plans.get(run.plan_id)
        if plan is None or plan.archived:
            continue
        failed.extend((run, task, plan.node(task.node_id)) for task in stuck)
    task_items = _items_as_stored(
        dstore,
        [task.item_id for _, task, _ in failed if task.item_id and task.item_id not in items],
    )
    for run, task, node in failed:
        item = None
        if task.item_id is not None:
            item = items.get(task.item_id) or task_items.get(task.item_id)
            if item is not None:
                # Read under the other spelling of its id: the listed row.
                item = items.get(item.item_id, item)
            seen = from_item(item)
            if seen is None or seen[0] != "failed":
                # The item has moved since the run's last pass (retried, or
                # its row is gone and the task is admitted again): the
                # driver follows it on its next one.
                continue
            # The task's failed item is the same waiting thing: one entry.
            if item is not None:
                items.pop(item.item_id, None)
        found.append(
            Waiting(
                "epic_task",
                "failed",
                task.updated_at,
                f"{run.id}:{task.node_id}",
                node.repository if node is not None else None,
                item=item,
                epic_run=run,
                task=task,
                node=node,
            )
        )

    for item in items.values():
        group: AttentionGroup = "decision" if item.state in PARKED_ITEM_STATES else "failed"
        found.append(
            Waiting(
                "item", group, item.updated_at, item_key(item), item_repository(item), item=item
            )
        )

    # What a manual plan waits on a person for, and what agents escalated:
    # after the items, so an item's own entry is the one found about it.
    found.extend(_plans_waiting(views, items))
    found.extend(_escalations(views))

    # A provider hold with no retry scheduled stands until a person
    # recovers it; one with a time to try again is the daemon's own wait.
    backend = str(views.config.agent.backend)
    hold = ProviderRecovery(views.store, backend, clock=views.ctx.clock).hold()
    if hold is not None and hold.next_at is None:
        found.append(
            Waiting(
                "provider_hold",
                "paused",
                hold.updated_at,
                f"{backend}:{hold.generation}",
                hold=hold,
            )
        )

    # A repository whose polling is suspended stays so until someone
    # resumes it; one backing off is tried again by itself. A named pause
    # hold is a person's own act and is not here.
    for health in views.status().get("repos") or []:
        if isinstance(health, dict) and health.get("suspended") and health.get("repo"):
            repo = str(health["repo"])
            found.append(
                Waiting("repository", "paused", health.get("since"), repo, repo, health=health)
            )
    return found


def _plans_waiting(views: Views, items: Mapping[str, WorkItem]) -> list[Waiting]:
    """What a ``manual`` plan waits on a person for: a breakdown's
    questions, and a proposed level to approve. A plan that advances
    itself has none here — what it needs reaches a person as an
    escalation.

    A breakdown's questions park its ``plan`` item ``awaiting_answers``,
    and that item's entry *is* the questions' entry: its id was on the
    list first and clients key on it. A ``plan_questions`` entry stands
    only for questions no such item stands for (the item is gone, or moved
    on while the plan still asks) — and never for questions whose item was
    dismissed."""
    plans = PlanStore(views.dstore).waiting_on_people()
    if not plans:
        return []
    found: list[Waiting] = []
    asked: set[tuple[str, str]] | None = None
    for plan in plans:
        for node in plan.nodes:
            questions = node.generation
            if questions is None or questions.status != "awaiting_answers":
                continue
            if asked is None:
                parked = [
                    *items.values(),
                    *views.dstore.attention_items(("awaiting_answers",), include_dismissed=True),
                ]
                asked = {
                    (str(item.plan_id), str(item.plan_node_id))
                    for item in parked
                    if item.state == "awaiting_answers" and item.plan_id
                }
            if (plan.id, node.id) in asked:
                continue
            found.append(
                Waiting(
                    PLAN_QUESTIONS,
                    "decision",
                    questions.asked_at or None,
                    f"{plan.id}:{node.id}:{questions.run_id}",
                    node.repository,
                    node=node,
                    plan=plan,
                )
            )
        if plan.generation_pending:
            continue
        for node in plan.nodes:
            proposed = [c for c in plan.children(node.id) if c.state == "proposed"]
            if not proposed:
                continue
            found.append(
                Waiting(
                    PLAN_PROPOSAL,
                    "decision",
                    min(c.created_at for c in proposed) or None,
                    f"{plan.id}:{node.id}",
                    node.repository,
                    node=node,
                    plan=plan,
                )
            )
    return found


def _escalations(views: Views) -> list[Waiting]:
    """Each escalation still waiting on a person. One whose target moved
    on is left out as soon as the list is read; the tracker's pass
    resolves it (:func:`lantern.api.escalations.settle`)."""
    return [
        Waiting(
            escalations.KIND,
            "decision",
            e.record.at,
            e.record.id,
            e.repository,
            item=e.item,
            plan=e.plan,
            escalation=e,
        )
        for e in escalations.unresolved(views.dstore)
    ]


def counts(found: Sequence[Waiting]) -> AttentionCounts:
    per_group = {group: sum(1 for w in found if w.group == group) for group in GROUPS}
    return AttentionCounts(total=len(found), **per_group)


def _advertise(actions: Iterable[str], principal: Principal | None) -> list[AttentionAction]:
    advertised = []
    for action in actions:
        capability = capability_for(action)
        advertised.append(
            AttentionAction(
                action=action,
                capability=capability,
                allowed=principal is not None and capability in principal.capabilities,
            )
        )
    return advertised


def _ordered(allowed: Iterable[str]) -> list[str]:
    held = set(allowed)
    return [action for action in ACTION_ORDER if action in held]


def entries(
    views: Views, page: Sequence[Waiting], auth: Authenticated | None
) -> list[AttentionEntry]:
    """The page as a client reads it, one entry for each of ``page`` in
    its order: public ids, references, the actions eligibility answers for
    each entry and whether the caller may take them. ``channel_id`` is set
    only where the caller can read the conversation, as the item listing
    does it. With no caller (the daemon reading its own list) no action is
    allowed and no conversation is named."""
    now = views.now
    principal = auth.principal if auth is not None else None
    items = list({w.item.item_id: w.item for w in page if w.item is not None}.values())
    gates = [w.gate for w in page if w.gate is not None]
    item_ids = views.ids.item_ids(items, now) if items else {}
    gate_ids = views.ids.gate_ids([gate.run_id for gate in gates], now) if gates else {}
    repos = sorted({w.repo for w in page if w.repo})
    repo_ids = views.ids.repository_ids(repos, now) if repos else {}
    runs = views.store.get_runs(
        [
            *(item.run_id for item in items if item.run_id),
            *(gate.run_id for gate in gates),
        ]
    )
    views.load_dismissals(item_ids=[item.item_id for item in items])
    if any(w.kind == "item" and w.item.state in REVIEW_WAIT_STATES for w in page if w.item):
        views.note_parked(holds=views.dstore.review_holds(_STANDING_HOLD_STATES))
    channels = views.visible_item_channels(items, auth.member) if auth is not None else {}

    def base(w: Waiting) -> dict[str, Any]:
        item = w.item
        return {
            "kind": w.kind,
            "group": w.group,
            "since": rfc3339(w.since),
            "repository": w.repo,
            "repository_id": repo_ids.get(w.repo) if w.repo else None,
            "item_id": item_ids[item_key(item)] if item is not None else None,
            "channel_id": channels.get(item.item_id) if item is not None else None,
        }

    out: list[AttentionEntry] = []
    for w in page:
        item = w.item
        if w.escalation is not None:
            out.append(_escalation_entry(w.escalation, base(w), principal))
        elif w.plan is not None and w.node is not None:
            out.append(_plan_entry(w, w.plan, w.node, base(w), principal))
        elif w.gate is not None:
            gate = w.gate
            run = runs.get(gate.run_id)
            allowed: set[str] = set()
            if "gate_approve" in views.gate_actions(gate, item, run):
                allowed.add("gate_approve")
            if item is not None and item.run_id == gate.run_id:
                allowed |= views.work_actions(item, run)
            unnamed = f"Approval in {w.repo}" if w.repo else "Approval requested"
            out.append(
                AttentionEntry(
                    **base(w),
                    id=gate_entry_id(gate_ids[gate.run_id]),
                    state="gated",
                    title=item.title if item is not None else unnamed,
                    reason=gate.detail,
                    run_id=run_public_id(gate.run_id),
                    gate_id=gate_ids[gate.run_id],
                    revision=gate.revision,
                    actions=_advertise(_ordered(allowed), principal),
                    dismissal=views.dismissal(item, gate.run_id) if item is not None else None,
                )
            )
        elif w.task is not None and w.epic_run is not None:
            task, epic_run = w.task, w.epic_run
            run_id = task.run_id or (item.run_id if item is not None else None)
            alert = f":{run_public_id(run_id)}" if run_id else ""
            out.append(
                AttentionEntry(
                    **base(w),
                    id=f"epic_task:{epic_run.id}:{task.node_id}{alert}",
                    state=task.state,
                    title=w.node.title if w.node is not None else task.node_id,
                    reason=task.reason,
                    run_id=run_public_id(run_id) if run_id else None,
                    plan_id=epic_run.plan_id,
                    node_id=task.node_id,
                    epic_run_id=epic_run.id,
                    actions=_advertise(TASK_ACTIONS, principal),
                )
            )
        elif item is not None:
            run = runs.get(item.run_id) if item.run_id else None
            public = item_ids[item_key(item)]
            alert = f":{run_public_id(item.run_id)}" if item.run_id else ""
            out.append(
                AttentionEntry(
                    **base(w),
                    id=f"item:{public}:{item.state}{alert}",
                    state=item.state,
                    title=item.title,
                    reason=item.last_error or (run.reason if run is not None else None),
                    run_id=run_public_id(item.run_id) if item.run_id else None,
                    plan_id=item.plan_id,
                    node_id=item.plan_node_id,
                    epic_run_id=item.parent_item_id if item.from_epic_run else None,
                    revision=item.revision,
                    actions=_advertise(_ordered(views.work_actions(item, run)), principal),
                    dismissal=views.dismissal(item),
                )
            )
        elif w.hold is not None:
            backend = w.hold.failure.backend
            out.append(
                AttentionEntry(
                    **base(w),
                    id=f"provider_hold:{w.key}",
                    state="provider_held",
                    title=f"The {backend} provider is held until someone recovers it",
                    reason=w.hold.summary(),
                    # No route releases a provider hold: it is recovered
                    # from the host or a chat (`resume <backend>`).
                    actions=[],
                )
            )
        elif w.health is not None:
            out.append(
                AttentionEntry(
                    **base(w),
                    id=f"repository:{repo_ids[w.key]}",
                    state="suspended",
                    title=f"{w.key} is no longer polled for work",
                    reason=str(w.health.get("reason") or "") or None,
                    actions=_advertise(REPOSITORY_ACTIONS, principal),
                )
            )
    return out


def _escalation_entry(
    escalation: escalations.Escalation,
    fields: dict[str, Any],
    principal: Principal | None,
) -> AttentionEntry:
    record = escalation.record
    needed = escalations.capability(record.action)
    return AttentionEntry(
        **fields,
        id=escalations.entry_id(record.id),
        state="escalated",
        title=escalations.title(record, escalation.target),
        reason=record.reason,
        run_id=run_public_id(record.run_id) if record.run_id else None,
        plan_id=record.plan_id,
        node_id=record.node_id,
        epic_run_id=record.epic_run_id,
        revision=escalation.revision,
        agent=record.agent_slug,
        decision_id=record.id,
        decision_action=record.action,
        actions=[
            AttentionAction(
                action=action,
                capability=needed,
                allowed=principal is not None and needed in principal.capabilities,
            )
            for action in escalations.actions(record.action)
        ],
    )


def _plan_entry(
    w: Waiting,
    plan: Plan,
    node: PlanNode,
    fields: dict[str, Any],
    principal: Principal | None,
) -> AttentionEntry:
    if w.kind == PLAN_QUESTIONS:
        asked = node.generation
        count = len(asked.unanswered()) if asked is not None else 0
        return AttentionEntry(
            **fields,
            id=f"{PLAN_QUESTIONS}:{w.key}",
            state="awaiting_answers",
            title=f"Questions about “{node.title}” wait for answers",
            reason=f"{count} question{'' if count == 1 else 's'} to answer on the plan",
            run_id=run_public_id(asked.run_id) if asked is not None else None,
            plan_id=plan.id,
            node_id=node.id,
            revision=plan.revision,
            # Answered on the plan's page, never from a notification.
            actions=[],
        )
    proposed = [c for c in plan.children(node.id) if c.state == "proposed"]
    noun = "epics" if proposed and proposed[0].level == "epic" else "tasks"
    by = sorted({c.proposed_by for c in proposed if c.proposed_by})
    holds = principal is not None and PROPOSAL_CAPABILITY in principal.capabilities
    # Approving is offered only to a caller who may approve. The daemon's
    # own read (no caller) lists it, so a reminder knows who that is.
    offered = PROPOSAL_ACTIONS if principal is None or holds else ()
    return AttentionEntry(
        **fields,
        id=f"{PLAN_PROPOSAL}:{w.key}",
        state="proposed",
        title=f"{len(proposed)} proposed {noun} under “{node.title}” wait for approval",
        reason=f"proposed by {', '.join(by)}" if by else None,
        plan_id=plan.id,
        node_id=node.id,
        revision=plan.revision,
        actions=[
            AttentionAction(action=action, capability=PROPOSAL_CAPABILITY, allowed=holds)
            for action in offered
        ],
    )


def find(
    views: Views, entry_id: str, auth: Authenticated | None, *, include_dismissed: bool = False
) -> AttentionEntry | None:
    """The entry ``entry_id`` names as it stands now, or ``None`` when
    nothing is waiting under that id. What is waiting of the id's own kind
    is matched by id with no caller in mind — a fixed number of statements
    however much waits — and only the match is projected for ``auth``, so
    looking one entry up does not read a conversation for every other."""
    kind = entry_id.partition(":")[0]
    found = [w for w in waiting(views, include_dismissed=include_dismissed) if w.kind == kind]
    for w, entry in zip(found, entries(views, found, None), strict=True):
        if entry.id == entry_id:
            return entry if auth is None else entries(views, [w], auth)[0]
    return None


def about(
    views: Views, *, run_id: str | None = None, item_id: str | None = None
) -> AttentionEntry | None:
    """The entry on the default list about the run ``run_id`` or, failing
    that, the item ``item_id`` (both by the ids the stores keep), as it
    stands now with no caller in mind — or ``None`` when neither is
    waiting. One read of what is waiting, and only the match projected."""
    if not run_id and not item_id:
        return None
    found = waiting(views)
    match = next((w for w in found if run_id and subject(w)[0] == run_id), None)
    if match is None and item_id:
        match = next((w for w in found if subject(w)[1] == item_id), None)
    return entries(views, [match], None)[0] if match is not None else None


def subject(w: Waiting) -> tuple[str | None, str | None]:
    """The run and the item an entry is about, by the ids the stores keep:
    what an event about the entry is recorded against, so it is shown to
    whoever is shown the work's own events."""
    item_id = w.item.item_id if w.item is not None else None
    if w.gate is not None:
        return w.gate.run_id, item_id
    if w.task is not None:
        return w.task.run_id or (w.item.run_id if w.item is not None else None), item_id
    return (w.item.run_id if w.item is not None else None), item_id
