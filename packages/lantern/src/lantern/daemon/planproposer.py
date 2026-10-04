"""Proposing: the planner drafting a plan from an owner's goal, under a
``plan.propose`` grant, with a person able to step in at every point.

The plan driver (:mod:`lantern.daemon.plandriver`) ticks this first, every
pass, under the same lock and against the same snapshot of the enabled
grants; it lives beside the driver rather than inside it because the
driver walks plans that exist and this makes them, and the two share
only the judging, the ledger and the backoff (which it borrows from the
driver). Off unless ``[delegation] propose_every`` is set: with ``0`` (the
default) it proposes nothing and never reads the forge; it only closes
a proposal escalation left open from when it was on.

Each pass, over the ``active`` goals oldest first, **at most one proposal
overall**. A goal is considered when:

* no plan serving it (``daemon_plans.goal_id``) is still open — not
  archived and not done (every epic of it on the forge and closed): one
  open plan per goal;
* ``propose_every`` has passed since the later of its last allowed
  proposal on the ledger and the last change to a plan that served it, so
  a restart neither forgets nor restarts the wait, and a plan archived or
  finished is followed by the next only a period later.

It is then judged — ``decide(agent="planner", action="plan.propose",
attrs={"repository", "level", "goal_id"})`` — before anything is read from
the forge. The goal's repository must be configured, enabled and able to
hold a plan, or the host escalates naming why. ``escalate`` and ``deny``
go to the ledger once per situation (the newest decision about the goal
is the reference, as the driver's are). Allowed, the repository's open
follow-up issues (``[landing] followup_label``) are read, newest first, at
most :data:`MAX_FOLLOWUPS`, dropping any whose origin marker is at or
beyond ``[agent_team] max_chain_depth``, and a draft plan is created as a
recorded ``plan.propose`` operation by ``agent:planner``: ``advance =
auto``, ``goal_id`` the goal's, and a brief — the goal's title and text,
and the follow-ups as ``title (url)`` — for the root to be generated from.
From there the driver's own steps (break down, review, approve, publish,
run) carry it, each under its own grant.

**The root's level** is decided from the goal alone, before the forge is
read, so a grant's ``levels`` condition judges what will be drafted: an
``initiative`` when the goal's text is at least
:data:`INITIATIVE_TEXT_CHARS` long (an objective that broad needs more than
one epic), an ``epic`` otherwise.

**The loop guard.** A proposed plan records its ``chain_depth`` on the
ledger: one more than the deepest follow-up its brief was built from (a
follow-up with no origin marker — filed by a run a person asked for — is
depth 0), so at least 1. Its epic runs' items carry it
(:meth:`~lantern.daemon.epicruns.EpicRunDriver._chain`), the follow-ups
their runs file carry it in their origin marker, and a follow-up at or
beyond ``max_chain_depth`` is never read into a brief again. So propose →
run → follow-up → propose stops after ``max_chain_depth`` generations of
follow-ups (2 by default); the goal itself is still proposed from at the
cadence, without them.

**A goal is never marked done here.** A goal whose plan finishes stays
``active`` — and is proposed from again a period later — until an owner
marks it ``done`` or ``paused``.

**Resolving.** An open ``plan.propose`` escalation is closed ``acted``
when the goal has an open plan again (the planner's, or one a person
drafted for it) and ``superseded`` when the goal is gone, or no longer
``active``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from lantern.agents.origin import origin_from_body
from lantern.daemon.controls.delegation import Decision, decide
from lantern.daemon.controls.delegation_store import DecisionRecord
from lantern.daemon.controls.operations import OperationSpec, record_plan_operation
from lantern.daemon.controls.principal import Principal
from lantern.errors import LanternError
from lantern.log import get_logger
from lantern.plans.service import PlanRefusal
from lantern.plans.store import PlanStore, new_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from lantern.daemon.goals import Goal, GoalPlan
    from lantern.daemon.plandriver import PlanDriver

log = get_logger(__name__)

#: Who proposes, and as whom the plan and its operation are recorded.
PROPOSER = "planner"
PROPOSED_BY = f"agent:{PROPOSER}"
ACTION = "plan.propose"

#: A goal whose text is at least this long is rooted at an initiative.
INITIATIVE_TEXT_CHARS = 1200
#: The most follow-up issues one brief names.
MAX_FOLLOWUPS = 10
#: How many pages (of 100) of the follow-up label are read, at most.
FOLLOWUP_PAGES = 3
#: The ``daemon_state`` failure key's node slot for a goal's proposal.
_GOAL_SLOT = "goal"


@dataclass(frozen=True, slots=True)
class Followup:
    """One open follow-up issue a brief may name."""

    number: int
    title: str
    url: str
    depth: int
    created_at: str


class _ProposeFailed(Exception):
    def __init__(self, detail: str, operation_id: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.operation_id = operation_id


def root_level(goal: Goal) -> str:
    """The level a plan proposed from ``goal`` is rooted at (see the module
    docstring): decided from the goal alone, deterministically."""
    return "initiative" if len(goal.text.strip()) >= INITIATIVE_TEXT_CHARS else "epic"


def plan_depth(followups: list[Followup]) -> int:
    """The chain depth of a plan briefed from ``followups``: one more than
    the deepest of them, at least 1."""
    return 1 + max((f.depth for f in followups), default=0)


def brief(goal: Goal, followups: list[Followup]) -> dict[str, Any]:
    """The sections the proposed plan's root is generated from."""
    sections: dict[str, Any] = {"title": goal.title, "goal": goal.text}
    if followups:
        lines = [
            "Open follow-up issues on this repository, newest first — "
            "fold in the ones that serve the goal:",
            "",
            *(f"- {f.title} ({f.url})" for f in followups),
        ]
        sections["context"] = "\n".join(lines)
    return sections


class PlanProposer:
    """Drafts plans from goals; one per plan driver."""

    def __init__(self, driver: PlanDriver) -> None:
        self.driver = driver
        self.loop = driver.loop
        self.store = PlanStore(driver.loop.dstore)

    # -- the pass --------------------------------------------------------------

    def tick(self, now: float) -> None:
        """One pass: the escalations it no longer stands at resolved, then
        at most one goal proposed for. Nothing is proposed, and the
        forge is not read, while ``propose_every`` is 0."""
        every = int(self.loop.config.delegation.propose_every)
        goals = getattr(self.loop, "goals", None)
        if goals is None:
            return
        self._resolve(now)
        if every <= 0:
            return
        active = goals.goals(state="active")
        if not active:
            return
        served = goals.plans_by_goal([goal.id for goal in active])
        for goal in active:
            try:
                if self._consider(goal, served.get(goal.id, []), every, now):
                    return
            except Exception:
                log.warning("plan_proposer.goal_failed", goal=goal.id, exc_info=True)

    def _consider(self, goal: Goal, plans: list[GoalPlan], every: int, now: float) -> bool:
        """Judge and, allowed, propose for ``goal``. ``True`` when a
        proposal was attempted (the pass's one)."""
        if any(plan.open and not self._done(plan.plan_id) for plan in plans):
            return False
        allowed = self.driver.delegation.latest_for_goal(goal.id, outcome="allow")
        marks = [plan.updated_at for plan in plans]
        if allowed is not None:
            marks.append(allowed.at)
        if marks and now < max(marks) + every:
            return False
        level = root_level(goal)
        attrs: dict[str, Any] = {
            "repository": goal.repository,
            "level": level,
            "goal_id": goal.id,
        }
        latest = self.driver.delegation.latest_for_goal(goal.id)
        host = self.driver._repository_problem(goal.repository)
        if host is not None:
            decision = Decision(outcome="escalate", reason=host)
        else:
            day_start = self.loop.usage_pool.day(now)[0]
            decision = decide(
                self.driver._grants,
                agent_slug=PROPOSER,
                action=ACTION,
                attrs=attrs,
                used_today=self.driver.delegation.used_today(day_start),
            )
        if decision.outcome != "allow":
            self._note(decision, goal, attrs, latest, now)
            return False
        key = (_GOAL_SLOT, goal.id, ACTION)
        failing = self.driver._failures(key)
        if failing is not None and now < self.driver.retry_at(*failing):
            return False
        try:
            followups = self._followups(goal.repository)
            plan_id, root_id, operation_id = self._create(goal, level, followups, now)
        except _ProposeFailed as failed:
            log.warning(
                "plan_proposer.propose_failed",
                goal=goal.id,
                error=failed.detail,
                failures=(0 if failing is None else failing[0]) + 1,
            )
            self.driver._failed(key, failing, now)
            self._note(
                Decision(
                    outcome="escalate",
                    reason=f"{decision.reason}, but it did not happen: {failed.detail}",
                ),
                goal,
                attrs,
                latest,
                now,
                operation_id=failed.operation_id,
            )
            return True
        self.driver._clear(key)
        self.driver.delegation.record(
            decision,
            agent_slug=PROPOSER,
            action=ACTION,
            attrs={
                **attrs,
                "chain_depth": plan_depth(followups),
                "followups": [f.number for f in followups],
            },
            now=now,
            plan_id=plan_id,
            node_id=root_id,
            repository=goal.repository,
            operation_id=operation_id,
        )
        if latest is not None and latest.unresolved:
            self.driver._close(latest, by=PROPOSED_BY, resolution="acted", now=now)
        log.info(
            "plan_proposer.proposed",
            goal=goal.id,
            plan=plan_id,
            level=level,
            followups=len(followups),
            grant=decision.grant_id,
            operation=operation_id,
        )
        return True

    def _done(self, plan_id: str) -> bool:
        """Whether a plan is finished: it has epics, and every one is on
        the forge and closed. Unreadable is not done."""
        plan = self.store.get(plan_id)
        if plan is None:
            return False
        epics = [node for node in plan.nodes if node.level == "epic"]
        return bool(epics) and all(
            node.state == "published" and node.forge is not None and node.forge.state == "closed"
            for node in epics
        )

    # -- reading the follow-ups ------------------------------------------------

    def _followups(self, repo: str) -> list[Followup]:
        """The open follow-up issues a brief may name, newest first, at
        most :data:`MAX_FOLLOWUPS`, each below ``[agent_team]
        max_chain_depth``. :class:`_ProposeFailed` when the forge is not
        up or its answer cannot be read: a brief built from a listing that
        could not be read is not proposed."""
        github = getattr(self.loop, "github", None)
        if github is None or not bool(getattr(github, "provisioned", True)):
            raise _ProposeFailed("the forge connection is not up yet")
        label = self.loop.config.landing.followup_label
        ceiling = int(self.loop.config.agent_team.max_chain_depth)
        try:
            ops = self.driver._connect()
            found: list[Followup] = []
            for page in range(1, FOLLOWUP_PAGES + 1):
                chunk = ops.issues_list(
                    repo,
                    labels=[label],
                    state="open",
                    sort="created",
                    direction="desc",
                    page=page,
                )
                if not isinstance(chunk, list):
                    raise _ProposeFailed("the follow-up listing was not a list")
                for issue in chunk:
                    entry = _followup(issue, label)
                    if entry is not None and entry.depth < ceiling:
                        found.append(entry)
                if len(chunk) < 100:
                    break
        except _ProposeFailed:
            raise
        except LanternError as exc:
            raise _ProposeFailed(f"the follow-up issues could not be read: {exc}") from exc
        except Exception as exc:
            raise _ProposeFailed(
                f"the follow-up issues could not be read: {type(exc).__name__}: {exc}"
            ) from exc
        unique = {f.number: f for f in found}.values()
        newest = sorted(unique, key=lambda f: (f.created_at, f.number), reverse=True)
        return newest[:MAX_FOLLOWUPS]

    # -- drafting --------------------------------------------------------------

    def _create(
        self, goal: Goal, level: str, followups: list[Followup], now: float
    ) -> tuple[str, str, str]:
        """The draft plan, as a recorded operation by the planner: its id,
        its root's id and the operation's id."""
        principal = Principal.for_agent(PROPOSER)
        actor = principal.audit()
        loop = self.loop
        plan_id = new_id("plan_")
        sections = brief(goal, followups)

        def create() -> Any:
            return loop.plans.create(
                level=level,
                repository=goal.repository,
                sections=sections,
                now=now,
                actor=actor,
                advance="auto",
                goal_id=goal.id,
                plan_id=plan_id,
            )

        spec = OperationSpec(
            action=ACTION,
            target_kind="plan",
            target_key=plan_id,
            principal=principal,
            request={
                "goal_id": goal.id,
                "repository": goal.repository,
                "level": level,
                "advance": "auto",
                "followups": [f.number for f in followups],
            },
        )
        try:
            operation_id, plan = record_plan_operation(
                loop.operations,
                spec,
                call=create,
                result=lambda made: {"plan_id": made.id, "revision": made.revision},
                clock=loop.clock,
                generation=getattr(loop, "generation", None),
            )
        except PlanRefusal as exc:
            raise _ProposeFailed(exc.detail, exc.extra.get("operation_id")) from exc
        except Exception as exc:
            raise _ProposeFailed(f"{type(exc).__name__}: {exc}") from exc
        return plan.id, plan.root_id, operation_id

    # -- the ledger ------------------------------------------------------------

    def _note(
        self,
        decision: Decision,
        goal: Goal,
        attrs: dict[str, Any],
        latest: DecisionRecord | None,
        now: float,
        *,
        operation_id: str | None = None,
    ) -> None:
        """Record an ``escalate`` or ``deny`` about ``goal`` — unless the
        newest decision about it already says the same about the same
        facts."""
        if (
            latest is not None
            and latest.outcome == decision.outcome
            and latest.reason == decision.reason
            and latest.attrs == attrs
        ):
            return
        if latest is not None and latest.unresolved:
            self.driver._close(latest, by=None, resolution="superseded", now=now)
        self.driver.delegation.record(
            decision,
            agent_slug=PROPOSER,
            action=ACTION,
            attrs=attrs,
            now=now,
            repository=goal.repository,
            operation_id=operation_id,
        )
        log.info(
            "plan_proposer.judged",
            goal=goal.id,
            outcome=decision.outcome,
            reason=decision.reason,
        )

    def _resolve(self, now: float) -> None:
        """Close each open proposal escalation the goal has moved past."""
        waiting = self.driver.delegation.unresolved_for_action(ACTION)
        if not waiting:
            return
        goals = self.loop.goals
        for escalation in waiting:
            goal_id = escalation.attrs.get("goal_id")
            goal = goals.goal(goal_id) if isinstance(goal_id, str) else None
            if goal is None or goal.state != "active":
                self.driver._close(escalation, by=None, resolution="superseded", now=now)
                continue
            open_plans = [
                plan
                for plan in goals.plans_for(goal.id)
                if plan.open and not self._done(plan.plan_id)
            ]
            if open_plans:
                by = self.store.get(open_plans[0].plan_id)
                self.driver._close(
                    escalation,
                    by=None if by is None else by.created_by,
                    resolution="acted",
                    now=now,
                )


def _followup(issue: object, label: str) -> Followup | None:
    """One listed issue as a follow-up, or ``None`` when it is not one (a
    pull request, closed, without the label). A malformed entry is a
    listing that cannot be read."""
    if not isinstance(issue, dict):
        raise _ProposeFailed("the follow-up listing held a malformed issue")
    if "pull_request" in issue:
        return None
    if str(issue.get("state") or "open") != "open":
        return None
    labels = {
        str(entry.get("name") if isinstance(entry, dict) else entry)
        for entry in issue.get("labels") or ()
    }
    if label not in labels:
        return None
    number = issue.get("number")
    title = str(issue.get("title") or "").strip()
    url = str(issue.get("html_url") or "").strip()
    if not isinstance(number, int) or isinstance(number, bool) or not title or not url:
        raise _ProposeFailed("the follow-up listing held an issue without a number, title or url")
    origin = origin_from_body(str(issue.get("body") or ""))
    depth = 0 if origin is None else origin.chain_depth
    return Followup(
        number=number,
        title=title,
        url=url,
        depth=depth,
        created_at=str(issue.get("created_at") or ""),
    )


__all__ = [
    "INITIATIVE_TEXT_CHARS",
    "MAX_FOLLOWUPS",
    "PROPOSED_BY",
    "PROPOSER",
    "PlanProposer",
    "brief",
    "plan_depth",
    "root_level",
]
