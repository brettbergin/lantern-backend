"""What an agent may decide without a person: grants, and the judge.

An owner states once that an agent may take an action under conditions —
a :class:`Grant` — and every time the daemon is about to act for that
agent it asks :func:`decide`, which answers one of three things:

* ``allow`` — an enabled grant covers the act; the grant is named.
* ``deny`` — the act is never an agent's to take, whatever the grants say.
* ``escalate`` — no grant covers it, or the judge could not tell; a person
  decides. An escalation waits; it never turns into a yes or a no.

An agent's capability set is not widened by any of this. A grant is not a
capability: it is a rule the daemon consults while it holds the resource
in hand, and the agent principal still carries ``items:create`` and
nothing else.

**The closed list.** Only the actions in :data:`DELEGABLE_ACTIONS` can be
named by a grant, and :func:`decide` denies anything else even when a row
names it. That list is what makes the rest non-delegable: editing grants,
managing credentials, managing the daemon and changing its configuration
are not on it, so no grant can hand them to an agent, and nothing here
needs a second list of what is forbidden.

**Conditions are data, never an expression language.** A grant's
:class:`Conditions` are six optional keys, each compared with one fact the
host supplies about the act. A key left out constrains nothing. Which keys
an action accepts:

====================  ============  ======  ============  ==============  ======  ===========
action                repositories  levels  max_children  require_review  causes  max_retries
====================  ============  ======  ============  ==============  ======  ===========
``plan.propose``      yes           yes
``plan.breakdown``    yes           yes
``plan.approve``      yes           yes     yes           yes
``plan.publish``      yes           yes     yes           yes
``plan.run``          yes                   yes
``plan.run.retry``    yes                                                 yes     yes
``item.retry``        yes                                                 yes     yes
``run.grant_rounds``  yes                                                 yes     yes
====================  ============  ======  ============  ==============  ======  ===========

A key that does not apply to the action is refused when the grant is
written, naming the key and the action. What each key compares:

* ``repositories`` — the act's ``repository`` is one of these (compared
  without regard to case).
* ``levels`` — the ``level`` of the plan node acted on is one of these.
* ``max_children`` — ``child_count`` is at most this.
* ``require_review`` — the level's stored ``review_verdict`` is
  ``approve``.
* ``causes`` — the ``failure_cause`` is one of these names.
* ``max_retries`` — ``retries``, the retries already made, is below this.

**Fail closed.** A condition whose fact is missing, or is a value the judge
cannot read (a level that is not a level, a count that is not a number, a
failure cause of ``unknown``), is not treated as met or unmet: the act
escalates and the reason names what was needed.

**No self-approval.** ``plan.approve`` is denied when the ``proposer`` is
the agent itself, and escalates when the proposer was not supplied — the
judge cannot rule self-approval out without it.

**Which grant, when several allow.** The oldest: by ``created_at``, then by
``id``. Never the order the caller handed them in.

Nothing here reads a store, a clock or the network. The grants, the facts
and how many times each grant already allowed something today are all
handed in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

Action = Literal[
    "plan.propose",
    "plan.breakdown",
    "plan.approve",
    "plan.publish",
    "plan.run",
    "plan.run.retry",
    "item.retry",
    "run.grant_rounds",
]

#: Every action a grant may name. Closed: nothing else can be delegated.
DELEGABLE_ACTIONS: tuple[str, ...] = get_args(Action)

DecisionOutcome = Literal["allow", "deny", "escalate"]
OUTCOMES: tuple[str, ...] = get_args(DecisionOutcome)

Level = Literal["initiative", "epic", "task"]
LEVELS: tuple[str, ...] = get_args(Level)

#: The verdict ``require_review`` asks for.
APPROVING_VERDICT = "approve"
#: A failure cause the host could not establish: never matched by a grant.
UNKNOWN_CAUSE = "unknown"

_PLAN_SCOPE = frozenset({"repositories", "levels"})
_PLAN_REVIEWED = frozenset({"repositories", "levels", "max_children", "require_review"})
_RETRY = frozenset({"repositories", "causes", "max_retries"})

#: The condition keys each action accepts (the table in the module docstring).
CONDITION_KEYS: dict[str, frozenset[str]] = {
    "plan.propose": _PLAN_SCOPE,
    "plan.breakdown": _PLAN_SCOPE,
    "plan.approve": _PLAN_REVIEWED,
    "plan.publish": _PLAN_REVIEWED,
    "plan.run": frozenset({"repositories", "max_children"}),
    "plan.run.retry": _RETRY,
    "item.retry": _RETRY,
    "run.grant_rounds": _RETRY,
}

#: The host-supplied fact each condition key is compared with.
ATTRIBUTE_FOR: dict[str, str] = {
    "repositories": "repository",
    "levels": "level",
    "max_children": "child_count",
    "require_review": "review_verdict",
    "causes": "failure_cause",
    "max_retries": "retries",
}

#: Every fact :func:`decide` reads; anything else in ``attrs`` is ignored.
ATTRIBUTES: tuple[str, ...] = (*ATTRIBUTE_FOR.values(), "proposer")


class GrantInvalid(ValueError):
    """A grant that cannot be written: ``field`` names what is wrong."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _names(value: object, what: str) -> tuple[str, ...]:
    """A non-empty list of non-empty names, trimmed, duplicates dropped."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"must be a list of {what}")
    out: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"must be a list of {what}; {entry!r} is not one")
        name = entry.strip()
        if name.casefold() not in seen:
            seen.add(name.casefold())
            out.append(name)
    if not out:
        raise ValueError(f"must name at least one of the {what}; leave it out for any")
    return tuple(out)


class Conditions(BaseModel):
    """What must hold for a grant to cover an act. Every key is optional;
    one left at its default constrains nothing."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    #: ``owner/name`` repositories the grant is limited to; ``None`` is any.
    repositories: tuple[str, ...] | None = None
    #: Plan levels the grant is limited to; ``None`` is any.
    levels: tuple[Level, ...] | None = None
    #: The most children the level acted on may have.
    max_children: int | None = Field(default=None, ge=1)
    #: The level's stored review verdict must be ``approve``.
    require_review: bool = False
    #: Failure causes the grant is limited to; ``None`` is any.
    causes: tuple[str, ...] | None = None
    #: The act is covered while fewer retries than this were already made.
    max_retries: int | None = Field(default=None, ge=1)

    @field_validator("repositories", mode="before")
    @classmethod
    def _repositories(cls, value: object) -> object:
        return None if value is None else _names(value, "repositories")

    @field_validator("levels", mode="before")
    @classmethod
    def _levels(cls, value: object) -> object:
        if value is None:
            return None
        levels = _names(value, "levels")
        unknown = [level for level in levels if level not in LEVELS]
        if unknown:
            raise ValueError(f"{unknown[0]!r} is not a level; the levels are {', '.join(LEVELS)}")
        return levels

    @field_validator("causes", mode="before")
    @classmethod
    def _causes(cls, value: object) -> object:
        if value is None:
            return None
        causes = _names(value, "failure causes")
        if any(cause.casefold() == UNKNOWN_CAUSE for cause in causes):
            raise ValueError(
                f"{UNKNOWN_CAUSE!r} cannot be listed: a failure whose cause could not be "
                "established always goes to a person"
            )
        return causes

    def set_keys(self) -> tuple[str, ...]:
        """The keys that constrain something, in the table's order."""
        return tuple(key for key in ATTRIBUTE_FOR if getattr(self, key) not in (None, False))

    def as_dict(self) -> dict[str, Any]:
        """Only the keys that constrain something: what is stored."""
        return {
            key: (list(value) if isinstance(value, tuple) else value)
            for key in self.set_keys()
            for value in (getattr(self, key),)
        }


class Grant(BaseModel):
    """A standing rule: ``agent_slug`` may take ``action`` while
    ``conditions`` hold, at most ``daily_limit`` times a day (``None`` is
    unlimited). ``revision`` counts its edits, for compare-and-set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    agent_slug: str
    action: str
    conditions: Conditions = Conditions()
    daily_limit: int | None = None
    enabled: bool = True
    note: str | None = None
    created_by: str | None = None
    created_by_display: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    revision: int = 1


class Decision(BaseModel):
    """The judge's answer: ``grant_id`` is set only when the outcome is
    ``allow``; ``reason`` is one plain sentence a person will read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: DecisionOutcome
    grant_id: str | None = None
    reason: str


def describe(grant: Grant) -> str:
    """A grant in one line, for the notice a write narrates."""

    def shown(value: object) -> str:
        if isinstance(value, tuple):
            return " or ".join(str(entry) for entry in value)
        return "set" if value is True else str(value)

    conditions = ", ".join(
        f"{key} is {shown(getattr(grant.conditions, key))}" for key in grant.conditions.set_keys()
    )
    limit = "any number of times" if grant.daily_limit is None else f"at most {grant.daily_limit}"
    return (
        f"{grant.agent_slug} may take {grant.action}"
        + (f" when {conditions}" if conditions else "")
        + f", {limit} a day"
        + ("" if grant.enabled else " (disabled)")
    )


# -- what may be written ------------------------------------------------------------


def check_action(action: object) -> None:
    """Refuse an action outside the closed list."""
    if not isinstance(action, str) or action not in DELEGABLE_ACTIONS:
        raise GrantInvalid(
            "action",
            f"{action!r} cannot be delegated; the actions a grant may name are "
            f"{', '.join(DELEGABLE_ACTIONS)}",
        )


def check_conditions(action: str, conditions: Conditions) -> None:
    """Refuse a condition key the action does not accept, naming both."""
    check_action(action)
    accepted = CONDITION_KEYS[action]
    for key in conditions.set_keys():
        if key not in accepted:
            raise GrantInvalid(
                f"conditions.{key}",
                f"condition {key!r} does not apply to {action}; it accepts "
                f"{', '.join(k for k in ATTRIBUTE_FOR if k in accepted)}",
            )


def parse_conditions(action: str, raw: Mapping[str, Any] | Conditions | None) -> Conditions:
    """``raw`` as conditions valid for ``action``. :class:`GrantInvalid`
    names the key that is unknown, malformed or not the action's."""
    if isinstance(raw, Conditions):
        conditions = raw
    else:
        try:
            conditions = Conditions.model_validate(dict(raw or {}))
        except ValidationError as exc:
            first = exc.errors()[0]
            key = ".".join(str(part) for part in first.get("loc", ())[:1])
            message = str(first.get("msg", "is not valid")).removeprefix("Value error, ")
            if first.get("type") == "extra_forbidden":
                message = f"is not a condition; the conditions are {', '.join(ATTRIBUTE_FOR)}"
            raise GrantInvalid(
                f"conditions.{key}" if key else "conditions",
                f"condition {key!r}: {message}" if key else f"conditions: {message}",
            ) from exc
    check_conditions(action, conditions)
    return conditions


def check_daily_limit(limit: object) -> None:
    """A daily limit is a positive whole number, or ``None`` for unlimited."""
    if limit is None:
        return
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise GrantInvalid(
            "daily_limit", "daily_limit must be a positive whole number, or null for no limit"
        )


# -- the judge ----------------------------------------------------------------------


def _agent(value: object) -> str | None:
    """An agent's slug from a slug or an ``agent:<slug>`` id, case folded;
    ``None`` for anything that is not text."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold().removeprefix("agent:")


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _could_not_tell(key: str, attrs: Mapping[str, object]) -> str:
    attribute = ATTRIBUTE_FOR[key]
    raw = attrs.get(attribute)
    seen = "it was not supplied" if raw is None else f"{raw!r} is not a value it can read"
    return f"could not tell whether {key} is met: it needs {attribute}, and {seen}"


def _shortfall(key: str, conditions: Conditions, attrs: Mapping[str, object]) -> str | None:
    """Why ``key`` does not hold for these facts; ``None`` when it does."""
    raw = attrs.get(ATTRIBUTE_FOR[key])
    if key == "repositories":
        repository = _text(raw)
        if repository is None:
            return _could_not_tell(key, attrs)
        allowed = conditions.repositories or ()
        if repository.casefold() not in {name.casefold() for name in allowed}:
            return f"repositories does not include {repository}"
        return None
    if key == "levels":
        level = _text(raw)
        if level is None or level not in LEVELS:
            return _could_not_tell(key, attrs)
        if level not in (conditions.levels or ()):
            return f"levels does not include {level}"
        return None
    if key == "max_children":
        children = _count(raw)
        if children is None:
            return _could_not_tell(key, attrs)
        if conditions.max_children is not None and children > conditions.max_children:
            return f"max_children is {conditions.max_children} and child_count is {children}"
        return None
    if key == "require_review":
        verdict = _text(raw)
        if verdict is None:
            return _could_not_tell(key, attrs)
        if verdict != APPROVING_VERDICT:
            return f"require_review is set and review_verdict is {verdict}"
        return None
    if key == "causes":
        cause = _text(raw)
        if cause is None or cause.casefold() == UNKNOWN_CAUSE:
            return _could_not_tell(key, attrs)
        if cause.casefold() not in {name.casefold() for name in conditions.causes or ()}:
            return f"causes does not include {cause}"
        return None
    retries = _count(raw)
    if retries is None:
        return _could_not_tell(key, attrs)
    if conditions.max_retries is not None and retries >= conditions.max_retries:
        return f"max_retries is {conditions.max_retries} and retries is {retries}"
    return None


def _falls_short(grant: Grant, attrs: Mapping[str, object], used: int) -> str | None:
    """Why ``grant`` does not allow the act; ``None`` when it does. Every
    condition that fails is named, and the budget is looked at only when
    the conditions hold."""
    problems = [
        problem
        for key in grant.conditions.set_keys()
        for problem in (_shortfall(key, grant.conditions, attrs),)
        if problem is not None
    ]
    if problems:
        return ", ".join(problems)
    if grant.daily_limit is not None and used >= grant.daily_limit:
        return f"its daily_limit of {grant.daily_limit} is spent"
    return None


def decide(
    grants: Iterable[Grant],
    *,
    agent_slug: str,
    action: str,
    attrs: Mapping[str, object],
    used_today: Mapping[str, int],
) -> Decision:
    """May ``agent_slug`` take ``action`` on what ``attrs`` describes?

    ``attrs`` are facts the host established (``repository``, ``level``,
    ``child_count``, ``proposer``, ``review_verdict``, ``failure_cause``,
    ``retries``), never anything the agent said about itself.
    ``used_today`` maps a grant's id to how many times it already allowed
    something in the current cap day. See the module docstring for the
    rules; this function reads nothing but its arguments.
    """
    if action not in DELEGABLE_ACTIONS:
        return Decision(
            outcome="deny",
            reason=f"{action or 'an unnamed action'} cannot be delegated to an agent",
        )
    agent = _agent(agent_slug) or ""
    if action == "plan.approve":
        proposer = _agent(attrs.get("proposer"))
        if proposer is None:
            return Decision(
                outcome="escalate",
                reason=(
                    "could not tell who proposed this level (proposer was not supplied, "
                    "or is not a name), "
                    f"so {agent_slug} approving it needs a person"
                ),
            )
        if proposer == agent:
            return Decision(
                outcome="deny",
                reason=f"{agent_slug} proposed this level, and an agent never approves its own",
            )
    candidates = sorted(
        (
            grant
            for grant in grants
            if grant.enabled and grant.action == action and _agent(grant.agent_slug) == agent
        ),
        key=lambda grant: (grant.created_at, grant.id),
    )
    if not candidates:
        return Decision(
            outcome="escalate", reason=f"no enabled grant lets {agent_slug} take {action}"
        )
    short: list[str] = []
    for grant in candidates:
        problem = _falls_short(grant, attrs, int(used_today.get(grant.id, 0)))
        if problem is None:
            return Decision(
                outcome="allow",
                grant_id=grant.id,
                reason=f"grant {grant.id} lets {agent_slug} take {action}",
            )
        short.append(f"grant {grant.id}: {problem}")
    return Decision(
        outcome="escalate",
        reason=f"no grant covers {agent_slug} taking {action} here ({'; '.join(short)})",
    )
