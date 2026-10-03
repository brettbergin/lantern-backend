"""Which controls apply to a run right now — and which never will.

A pure function of the run's kind and state, its work item's state, the
gate or review hold parked on it, and whether the version-control backend
can act on the target's forge. A read surface advertises the result as
``available_actions``; a mutation re-checks it at the moment it acts. The
loop keeps its own refusals (it holds the locks); this module is what a
surface can answer *before* asking, and it is deliberately stricter than
permissive: an answer it cannot give is "no", with the reason named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from lantern.daemon.controls.results import ControlError
from lantern.engine.model import RESUMABLE_RUN_STATES, TERMINAL_RUN_STATES, RunKind
from lantern.vcs.protocol import Capability

Action = Literal[
    "cancel",
    "resume",
    "steer",
    "grant_rounds",
    "gate_approve",
    "review_wait_resume",
    "retry",
    "requeue",
    "abandon",
    "dismiss",
    "undismiss",
    "delete",
]
ACTIONS: tuple[Action, ...] = get_args(Action)

#: Controls a fixed tool recipe never has: no agent to steer, no fix rounds
#: to grant, no gate to release — it runs one command and reports.
_NOT_FOR_TOOLS: frozenset[Action] = frozenset({"steer", "grant_rounds", "gate_approve"})
#: Controls that act on the target's forge and so need the backend able
#: to do so; a backend that cannot tell refuses them.
_FORGE_ACTIONS: frozenset[Action] = frozenset({"gate_approve", "review_wait_resume", "retry"})
#: The item transitions the store accepts (``DaemonStore.abandon`` /
#: ``retry`` / ``requeue``), mirrored so a surface can answer before asking.
_ITEM_TRANSITIONS: dict[Action, frozenset[str]] = {
    "abandon": frozenset(
        {
            "queued",
            "running",
            "blocked",
            "gated",
            "awaiting_review",
            "paused_review",
            "awaiting_answers",
        }
    ),
    "retry": frozenset({"failed", "blocked", "cancelled", "queued"}),
    "requeue": frozenset({"running", "queued"}),
}
_RESUMABLE_ITEM_STATES: frozenset[str] = frozenset({"queued", "cancelled", "failed"})
#: Where finished work rests. Only work at rest can be deleted: work that is
#: queued, running or parked on a decision is settled first (abandoned or
#: cancelled), so a delete never doubles as a way to stop something.
_RESTING_ITEM_STATES: frozenset[str] = frozenset({"done", "failed", "blocked", "cancelled"})
_RESTING_RUN_STATES: frozenset[str] = frozenset(
    {"merged", "completed", "failed", "blocked", "cancelled"}
)
#: The states that ask a person to look: finished without success, or parked
#: on a decision. Only these have an alert to dismiss — work that is queued,
#: running or done has nothing to acknowledge.
_ATTENTION_ITEM_STATES: frozenset[str] = frozenset(
    {
        "failed",
        "blocked",
        "cancelled",
        "gated",
        "awaiting_review",
        "paused_review",
        "awaiting_answers",
    }
)
_ATTENTION_RUN_STATES: frozenset[str] = frozenset(
    {
        "failed",
        "blocked",
        "cancelled",
        "gated",
        "awaiting_review",
        "held",
        "provider_held",
        "awaiting_answers",
    }
)


@dataclass(frozen=True, slots=True)
class Subject:
    """What is known about the target when the question is asked.

    ``None`` for a field means "no such thing" (no gate, no hold, no run
    yet), never "unknown": a caller that cannot fill a field must not ask.
    ``forge`` is the backend's answer to "can you act on this target's
    forge" — ``None`` when the action would not touch it.
    """

    run_kind: RunKind = "code"
    run_state: str | None = None
    item_state: str | None = None
    #: The run is the one in flight in the daemon.
    is_current: bool = False
    #: The run is the pinned run of its work item.
    pinned: bool = True
    exhausted: bool = False
    gate_state: str | None = None
    review_hold_state: str | None = None
    forge: Capability | None = None
    #: A person already dismissed the alert this work raises.
    dismissed: bool = False
    #: A person deleted the work: it is hidden, and nothing more applies.
    deleted: bool = False


def check(action: Action, subject: Subject) -> None:
    """Raise :class:`ControlError` when ``action`` does not apply."""
    if action in _NOT_FOR_TOOLS and subject.run_kind == "tool":
        raise ControlError("unsupported_for_kind", f"a tool run has no {action.replace('_', ' ')}")
    if action in _FORGE_ACTIONS and subject.forge is not None:
        if subject.forge is Capability.UNKNOWN:
            raise ControlError(
                "capability_unknown",
                "the version-control backend could not say whether it can act on the "
                "target; refusing rather than guessing",
            )
        if subject.forge is Capability.UNSUPPORTED:
            raise ControlError(
                "capability_unsupported",
                "the version-control backend cannot act on the target",
            )
    if subject.deleted:
        raise ControlError("not_eligible", "work was deleted")
    refusal = _refusal(action, subject)
    if refusal is not None:
        raise ControlError("not_eligible", refusal)


def available_actions(subject: Subject) -> frozenset[Action]:
    """Every action :func:`check` would allow."""
    allowed: set[Action] = set()
    for action in ACTIONS:
        try:
            check(action, subject)
        except ControlError:
            continue
        allowed.add(action)
    return frozenset(allowed)


def _refusal(action: Action, s: Subject) -> str | None:
    state = s.run_state
    if action == "cancel":
        if s.is_current:
            return None
        if state is None:
            return "no run to cancel"
        if state in TERMINAL_RUN_STATES and not (s.item_state == "queued" and s.pinned):
            return f"run is {state}"
        return None
    if action == "resume":
        if s.is_current:
            return "run is in flight"
        if state is None or state not in RESUMABLE_RUN_STATES:
            return f"run is {state}" if state else "no run to resume"
        if not s.pinned or s.item_state not in _RESUMABLE_ITEM_STATES:
            return f"work item is {s.item_state}"
        return None
    if action == "steer":
        return None if s.is_current else "run is not in flight"
    if action == "grant_rounds":
        if state != "failed" or not s.exhausted:
            return "run did not exhaust its fix rounds"
        if not s.pinned or s.item_state == "running":
            return f"work item is {s.item_state}"
        return None
    if action == "gate_approve":
        if s.gate_state is None:
            return "nothing is awaiting approval"
        if s.gate_state != "open":
            return f"gate is {s.gate_state}"
        return None
    if action == "review_wait_resume":
        if s.review_hold_state is None:
            return "not waiting for a review"
        if s.review_hold_state not in ("open", "paused"):
            return f"review wait is {s.review_hold_state}"
        return None
    if action in _ITEM_TRANSITIONS:
        if s.item_state is None:
            return "no work item"
        if s.item_state not in _ITEM_TRANSITIONS[action]:
            return f"work item is {s.item_state}"
        return None
    if action == "dismiss":
        if s.dismissed:
            return "already dismissed"
        return _nothing_to_dismiss(s)
    if action == "undismiss":
        return None if s.dismissed else "not dismissed"
    if action == "delete":
        return _not_deletable(s)
    return None


def _not_deletable(s: Subject) -> str | None:
    """Why the work cannot be deleted yet, or ``None`` when it can: it must
    be at rest. The work item decides when it carries the work, as for a
    dismissal."""
    if s.is_current:
        return "run is in flight"
    if s.item_state is not None and (s.pinned or s.run_state is None):
        if s.item_state in _RESTING_ITEM_STATES:
            return None
        return f"work item is {s.item_state}; abandon it first"
    if s.run_state is None:
        return "no work to delete"
    if s.run_state not in _RESTING_RUN_STATES:
        return f"run is {s.run_state}; cancel it first"
    return None


def _nothing_to_dismiss(s: Subject) -> str | None:
    """Why the work raises no alert a person could dismiss, or ``None``
    when it does. The work item decides when it carries the work — it pins
    the run, or there is no run yet; a run nothing pins (its item row is
    gone, or has moved on to a later attempt) is judged on its own state."""
    if s.is_current:
        return "run is in flight"
    if s.item_state is not None and (s.pinned or s.run_state is None):
        if s.item_state in _ATTENTION_ITEM_STATES:
            return None
        if s.item_state == "queued" and s.pinned and s.run_state == "provider_held":
            # Parked on a provider outage: the item waits in the queue, the
            # run is what asks for attention.
            return None
        return f"nothing needs attention: work item is {s.item_state}"
    if s.run_state is None:
        return "no work to dismiss"
    if s.run_state not in _ATTENTION_RUN_STATES:
        return f"nothing needs attention: run is {s.run_state}"
    return None
