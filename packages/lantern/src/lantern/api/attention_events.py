"""``attention.opened`` and ``attention.resolved``: the chronology says when
something starts waiting on a person and when it stops.

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

**What it costs.** It is called on every pass of the projector, once a
second, and looks at the list only when the daemon itself recorded
something that could have changed it since the last pass
(:data:`TRIGGERS` — never a run's own projected output, never a chat's
traffic), or when :data:`SWEEP_S` has gone by since it last looked, for
the changes that record nothing (polling that stopped, a provider hold).
A pass with nothing new runs no statement.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from lantern.api.attention import entries, subject, waiting
from lantern.api.chronology import DAEMON_ACTOR
from lantern.api.models import AttentionEntry
from lantern.api.projections import Views
from lantern.log import get_logger

if TYPE_CHECKING:
    from lantern.api.context import ApiContext

log = get_logger(__name__)

OPENED = "attention.opened"
RESOLVED = "attention.resolved"

_STATE_PREFIX = "attention."
#: ``daemon_state`` keys of the entries last seen waiting: the prefix, then
#: the entry's id. The value is what its events carry.
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
        seen, self._seen = self._seen, mark
        if self._swept is not None and now - self._swept < self.sweep_s:
            if mark == seen:
                return 0
            if not ctx.chronology.recorded_after(seen or 0, TRIGGERS):
                return 0
        self._swept = now
        return self.diff(now)

    def diff(self, now: float) -> int:
        """Compare the list with the set last seen and record the
        difference; the first time, record the set and say nothing."""
        views = Views(self.ctx)
        dstore = views.dstore
        found = sorted(waiting(views), key=lambda w: w.order)
        current: dict[str, dict[str, Any]] = {}
        for w, entry in zip(found, entries(views, found, None), strict=True):
            run_id, item_id = subject(w)
            current[entry.id] = {"run_id": run_id, "item_id": item_id, "data": event_data(entry)}
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
