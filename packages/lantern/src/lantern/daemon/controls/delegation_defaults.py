"""The grants every installation starts with.

Grants do not ship empty: the daemon seeds :data:`DEFAULT_GRANTS` when it
starts, enabled, so an owner only has to think about adding to them. Each
default is seeded once, ever. Its ``default_key`` is recorded in
``daemon_state`` (:data:`SEEDED_PREFIX`) in the same transaction that
writes the grant, and a key recorded there is never seeded again — not
when the owner deleted the grant (the record is the tombstone), and not
when the owner edited or paused it (an existing grant is never touched).
``POST /v1/grants/defaults/restore`` is how an owner brings a deleted
default back.

What the defaults let happen, and when:

* The plan defaults (``planner`` breaks a level down and proposes plans;
  ``critic`` approves, publishes and starts a reviewed level) act only on
  a plan a person set to ``advance = auto``, and proposing also needs
  ``[delegation] propose_every`` and an active goal. A manual plan, and
  every ``code``, ``workload`` and ``tool`` run, is untouched by them.
* The triage defaults act as soon as the daemon runs: ``operator``
  retries an item that failed in the last day on a ``ci_timeout``,
  ``forge_transient`` or ``provider_throttle`` once, at most five a day,
  and grants more fix rounds once to a run that spent its review or CI
  rounds, at most three a day.

There is no default token budget: ``[daemon] daily_token_budget`` stays
unset, and each grant's ``daily_limit`` is the spend guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lantern.daemon.controls.delegation import Conditions, parse_conditions

#: ``daemon_state`` key prefix: ``<prefix><default_key>`` is set once that
#: default was seeded.
SEEDED_PREFIX = "delegation.defaults.seeded:"
#: Who a seeded grant names as its author.
SEEDED_BY = "lantern"
SEEDED_BY_DISPLAY = "Lantern default"


@dataclass(frozen=True, slots=True)
class DefaultGrant:
    """One default: the stable ``key`` it is seeded under and the grant."""

    key: str
    agent_slug: str
    action: str
    daily_limit: int
    note: str
    conditions: dict[str, Any] = field(default_factory=dict)

    def parsed(self) -> Conditions:
        """The conditions, validated for the action as an owner's would be."""
        return parse_conditions(self.action, self.conditions)


#: The default set, in the order ``GET /v1/grants`` lists it.
DEFAULT_GRANTS: tuple[DefaultGrant, ...] = (
    DefaultGrant(
        key="plan.breakdown:planner:v1",
        agent_slug="planner",
        action="plan.breakdown",
        conditions={"levels": ["epic", "task"]},
        daily_limit=10,
        note="Lantern default: the planner breaks down a level of a plan set to Auto.",
    ),
    DefaultGrant(
        key="plan.approve:critic:v1",
        agent_slug="critic",
        action="plan.approve",
        conditions={"require_review": True, "max_children": 8},
        daily_limit=5,
        note="Lantern default: the critic approves a reviewed level of up to 8 children "
        "on a plan set to Auto.",
    ),
    DefaultGrant(
        key="plan.publish:critic:v1",
        agent_slug="critic",
        action="plan.publish",
        conditions={"require_review": True, "max_children": 8},
        daily_limit=5,
        note="Lantern default: the critic publishes an approved, reviewed level of up to "
        "8 children on a plan set to Auto.",
    ),
    DefaultGrant(
        key="plan.run:critic:v1",
        agent_slug="critic",
        action="plan.run",
        conditions={"max_children": 12},
        daily_limit=3,
        note="Lantern default: the critic starts a published epic of up to 12 tasks on a "
        "plan set to Auto.",
    ),
    DefaultGrant(
        key="plan.propose:planner:v1",
        agent_slug="planner",
        action="plan.propose",
        daily_limit=2,
        note="Lantern default: the planner proposes a plan from an active goal when "
        "[delegation] propose_every is set.",
    ),
    DefaultGrant(
        key="item.retry:operator:v1",
        agent_slug="operator",
        action="item.retry",
        conditions={
            "causes": ["ci_timeout", "forge_transient", "provider_throttle"],
            "max_retries": 1,
        },
        daily_limit=5,
        note="Lantern default: triage retries a recent failure once when its cause was transient.",
    ),
    DefaultGrant(
        key="run.grant_rounds:operator:v1",
        agent_slug="operator",
        action="run.grant_rounds",
        conditions={
            "causes": ["review_rounds_exhausted", "ci_rounds_exhausted"],
            "max_retries": 1,
        },
        daily_limit=3,
        note="Lantern default: triage grants more fix rounds once to a run that spent "
        "its review or CI rounds.",
    ),
)

DEFAULT_KEYS: tuple[str, ...] = tuple(default.key for default in DEFAULT_GRANTS)


def default_order(key: str | None) -> int:
    """Where a default sits in the table; a key no longer in the table
    sorts after every one that is."""
    return DEFAULT_KEYS.index(key) if key in DEFAULT_KEYS else len(DEFAULT_KEYS)
