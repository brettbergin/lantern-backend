"""``attention.opened``, ``attention.resolved`` and ``attention.reminder``:
the chronology says when something starts waiting on a person, when it
stops, and — while it waits — that it is still waiting.

``GET /v1/attention`` is computed on read, so nothing told a client — or
the push dispatcher, or a reminder — that an entry had appeared or gone:
each polled the list and compared. The tracker here makes that comparison
once, for everyone, and records the difference as two durable events
whose ``entry_id`` is exactly the list's ``id``.

**What it remembers.** The set of entries last seen on the default list
(dismissed alerts left out) is kept in ``daemon_state``, one value per
entry under :data:`OPEN_PREFIX`, each written or removed in the same
transaction as the event that announces it. A restart therefore neither
announces what is still open a second time nor loses the resolution of
what settled while nothing was watching. The first pass ever — an upgrade
— finds no :data:`SEEDED_KEY` and records what is waiting without a word.

**Reminders.** An entry never expires to yes or to no; it waits until
someone acts, and people are reminded. Each kept value also carries when
the entry was first announced, when it was last reminded about and how
many times. On the same passes an entry open at least
``[attention] remind_after_s`` and not reminded about within
``remind_every_s`` gets one :data:`REMINDER` — the opening's data plus
``waiting_s``, ``reminders``, its ``actions`` and the ``capabilities``
they need — and the clock moves in the event's own transaction, so a
restart repeats nothing and a daemon that was down for a week sends one
reminder on return, not seven. A value the release before reminders wrote has no
clock; it is stamped as first seen now, never overdue.

**What it costs.** It is called on every pass of the projector, once a
second, and looks at the list only when the daemon itself recorded
something that could have changed it since the last pass
(:data:`TRIGGERS` — never a run's own projected output, never a chat's
traffic), or when :data:`SWEEP_S` has gone by since it last looked, for
the changes that record nothing (polling that stopped, a provider hold).
A pass with nothing new runs no statement; reminders are judged on the
passes that read the list anyway, and a sweep with none due writes nothing.

**The daily digest** (:mod:`lantern.api.digest`) rides the same passes:
with ``[attention] digest_at`` set, the first pass at or after that time
of day records the day's ``briefing.digest``; unset, it costs nothing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lantern.api import escalations
from lantern.api.attention import entries, subject, waiting
from lantern.api.chronology import DAEMON_ACTOR
from lantern.api.digest import Digest
from lantern.api.models import AttentionEntry
from lantern.api.projections import Views
from lantern.log import get_logger

if TYPE_CHECKING:
    from lantern.api.context import ApiContext

log = get_logger(__name__)

OPENED = "attention.opened"
RESOLVED = "attention.resolved"
REMINDER = "attention.reminder"

_STATE_PREFIX = "attention."
#: ``daemon_state`` keys of the entries last seen waiting: the prefix, then
#: the entry's id. The value is what its events carry (``run_id``,
#: ``item_id``, ``data``) and the reminder clock (``opened_at``,
#: ``reminded_at``, ``reminders``).
OPEN_PREFIX = _STATE_PREFIX + "open:"
#: Set once the first pass has recorded what was already waiting.
SEEDED_KEY = _STATE_PREFIX + "seeded"

#: The daemon's own events that can change what is waiting: a command, a
#: run starting or ending, a gate, a notice, a plan or an epic run moving.
TRIGGERS: tuple[str, ...] = ("operation.", "run.", "gate.", "daemon.", "plan.")
#: How long a change that recorded none of those can go unnoticed.
SWEEP_S = 60.0


def event_data(entry: AttentionEntry) -> dict[str, Any]:
    """What an event says of an entry: its id and the fields of the list
    that are the same for every reader. ``channel_id`` and ``actions``
    depend on who reads, and are the list's to answer."""
    return {
        "entry_id": entry.id,
        "kind": entry.kind,
        "group": entry.group,
        "state": entry.state,
        "title": entry.title,
        "since": entry.since,
        "repository": entry.repository,
        "repository_id": entry.repository_id,
        "item_id": entry.item_id,
        "run_id": entry.run_id,
        "gate_id": entry.gate_id,
        "plan_id": entry.plan_id,
        "node_id": entry.node_id,
        "epic_run_id": entry.epic_run_id,
        "revision": entry.revision,
    }


class AttentionTracker:
    """Records an entry appearing on, and leaving, the default list."""

    def __init__(self, ctx: ApiContext, *, sweep_s: float = SWEEP_S) -> None:
        self.ctx = ctx
        self.sweep_s = sweep_s
        #: The daily digest rides the same passes (``[attention] digest_at``).
        self.digest = Digest(ctx)
        #: The chronology's high-water mark at the last pass.
        self._seen: int | None = None
        #: When the list was last read; ``None`` until it has been.
        self._swept: float | None = None

    def step(self, now: float, mark: int | None) -> int:
        """One pass, handed the chronology's high-water mark: read the
        list and record what changed, if anything could have. Returns how
        many events were recorded."""
        ctx = self.ctx
        if not ctx.ready.is_set() or ctx.stopping.is_set():
            # Recovery is still settling what the last process left.
            return 0
        try:
            digested = self.digest.step(now)
        except Exception:
            log.warning("attention.digest_failed", exc_info=True)
            digested = 0
        seen, self._seen = self._seen, mark
        if self._swept is not None and now - self._swept < self.sweep_s:
            if mark == seen:
                return digested
            if not ctx.chronology.recorded_after(seen or 0, TRIGGERS):
                return digested
        self._swept = now
        return digested + self.diff(now)

    def diff(self, now: float) -> int:
        """Compare the list with the set last seen and record the
        difference, and a reminder for what has waited long enough; the
        first time, record the set and say nothing."""
        views = Views(self.ctx)
        dstore = views.dstore
        # An escalation whose target is gone is off the list as soon as it
        # is read; here it is resolved ``superseded`` in the ledger, so the
        # list read never writes. One whose step moved on is the plan
        # driver's or triage's to resolve, never this pass's.
        settled = escalations.settle(dstore, now)
        if settled:
            log.info("attention.escalations_superseded", decisions=[d.id for d in settled])
        found = sorted(waiting(views), key=lambda w: w.order)
        current: dict[str, dict[str, Any]] = {}
        capabilities: dict[str, list[str]] = {}
        actions: dict[str, list[str]] = {}
        needs: dict[str, dict[str, str]] = {}
        for w, entry in zip(found, entries(views, found, None), strict=True):
            run_id, item_id = subject(w)
            current[entry.id] = _opened(run_id, item_id, event_data(entry), now)
            capabilities[entry.id] = sorted({action.capability for action in entry.actions})
            actions[entry.id] = [action.action for action in entry.actions]
            needs[entry.id] = {action.action: action.capability for action in entry.actions}
        kept: dict[str, str] = dstore.values_with_prefix(_STATE_PREFIX)
        known = {
            key[len(OPEN_PREFIX) :]: value
            for key, value in kept.items()
            if key.startswith(OPEN_PREFIX)
        }
        if SEEDED_KEY not in kept:
            for entry_id, record in current.items():
                if entry_id not in known:
                    dstore.set_value(OPEN_PREFIX + entry_id, json.dumps(record))
            dstore.set_value(SEEDED_KEY, repr(now))
            log.info("attention.seeded", waiting=len(current))
            return 0
        recorded = 0
        for entry_id, record in current.items():
            if entry_id not in known:
                self._record(OPENED, now, record, {OPEN_PREFIX + entry_id: json.dumps(record)})
                recorded += 1
        for entry_id, raw in known.items():
            if entry_id not in current:
                self._record(RESOLVED, now, _kept(entry_id, raw), {OPEN_PREFIX + entry_id: None})
                recorded += 1
        reminders = self.ctx.config.attention
        for entry_id, raw in known.items():
            if entry_id not in current:
                continue
            record = _kept(entry_id, raw)
            if not isinstance(record.get("opened_at"), int | float):
                # Kept by a release without the clock (or unreadable):
                # first seen now, with what the list says of it now.
                record = {**current[entry_id], "data": record.get("data") or {}}
                if set(record["data"]) <= {"entry_id"}:
                    record["data"] = current[entry_id]["data"]
                dstore.set_value(OPEN_PREFIX + entry_id, json.dumps(record))
                continue
            if not reminders.enabled:
                continue
            opened_at = float(record["opened_at"])
            reminded_at = record.get("reminded_at")
            last = float(reminded_at) if isinstance(reminded_at, int | float) else None
            if now - opened_at < reminders.remind_after_s:
                continue
            if last is not None and now - last < reminders.remind_every_s:
                continue
            count = int(record.get("reminders") or 0) + 1
            reminded = {**record, "reminded_at": now, "reminders": count}
            self._record(
                REMINDER,
                now,
                {
                    **current[entry_id],
                    "data": {
                        **current[entry_id]["data"],
                        "waiting_s": int(now - opened_at),
                        "reminders": count,
                        "capabilities": capabilities[entry_id],
                        "actions": actions[entry_id],
                        # What each action needs: an escalation's approve
                        # and decline need the escalated act's capability.
                        "action_capabilities": needs[entry_id],
                    },
                },
                {OPEN_PREFIX + entry_id: json.dumps(reminded)},
            )
            recorded += 1
        return recorded

    def _record(
        self, type_: str, now: float, record: dict[str, Any], state: dict[str, str | None]
    ) -> None:
        """One event and the set's own change, together. It is recorded
        against the entry's run and item, so it is shown to whoever is
        shown that work's events."""
        self.ctx.chronology.record(
            type_,
            now,
            run_id=record.get("run_id"),
            item_id=record.get("item_id"),
            actor=DAEMON_ACTOR,
            data=dict(record.get("data") or {}),
            state=state,
        )


def _opened(
    run_id: str | None, item_id: str | None, data: dict[str, Any], now: float
) -> dict[str, Any]:
    """The value kept for an entry that opens now."""
    return {
        "run_id": run_id,
        "item_id": item_id,
        "data": data,
        "opened_at": now,
        "reminded_at": None,
        "reminders": 0,
    }


def _kept(entry_id: str, raw: str) -> dict[str, Any]:
    """What was kept of an entry when it opened; an unreadable value
    still names the entry it was kept for."""
    try:
        record = json.loads(raw)
    except ValueError:
        record = None
    if not isinstance(record, dict) or not isinstance(record.get("data"), dict):
        return {"data": {"entry_id": entry_id}}
    return record
