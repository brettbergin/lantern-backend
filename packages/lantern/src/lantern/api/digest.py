"""The daily digest: once a day, unasked, what happened.

A person who stops watching hears nothing until something waits on them;
the briefing (``GET /v1/briefing``) answers only when asked. At
``[attention] digest_at`` — a local time of day in ``[daemon]
run_cap_timezone`` — the attention tracker's pass computes the briefing
since the previous digest (a day, the first time) with the route's own
code, as the summary anyone may read (no ``decided.recent``), and records:

- one ``briefing.digest`` event with the numbers only — ``landed``,
  ``failed``, ``waiting``, ``decided_allow``, ``decided_escalate``,
  ``runway_days``, ``since``, ``until``, the ``day`` and the ``timezone`` it
  was said in; no titles, no reasons. It names no run, item or channel, so
  every member is shown it, and the push rules turn it into one ``work``
  push per member;
- one control-channel line (``daemon.digest``, ``info``) through the
  daemon's frontend, as the budget notice is: the same summary in words.

**Once a day, never a burst.** The day of the last digest and when it was
sent are kept in ``daemon_state`` under :data:`STATE_KEY`, written in the
event's own transaction. A restart therefore sends nothing twice; a daemon
that was down at the digest time sends it once when it is back the same
day; a day it missed entirely is skipped, not made up — the next digest
simply covers everything since the last one.

**What it costs.** With ``digest_at`` empty, a pass reads one attribute.
With it set, a pass before the time compares a clock; after the day's
digest is sent, the day is remembered in memory and the pass runs no
statement.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lantern.api.chronology import DAEMON_ACTOR
from lantern.daemon.model import DaemonNotice, NoticeKind
from lantern.log import get_logger

if TYPE_CHECKING:
    from lantern.api.context import ApiContext

log = get_logger(__name__)

#: The event type.
DIGEST = "briefing.digest"
#: The control-channel notice's kind.
NOTICE: NoticeKind = "daemon.digest"
#: ``daemon_state``: ``{"day": "<local date>", "at": <epoch>}`` of the last.
STATE_KEY = "briefing.digest.last"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _count(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _when(since: datetime, until: datetime, zone: ZoneInfo) -> str:
    """When ``since`` was, said from ``until``'s day: today, yesterday, or
    the date."""
    start, end = since.astimezone(zone), until.astimezone(zone)
    clock = f"{start:%H:%M}"
    if start.date() == end.date():
        return f"today {clock}"
    if start.date() == end.date() - timedelta(days=1):
        return f"yesterday {clock}"
    return f"{start:%a %d %b} {clock}"


def _days(value: float) -> str:
    rounded = round(value, 1)
    text = str(int(rounded)) if rounded == int(rounded) else f"{rounded:.1f}"
    return f"{text} day" + ("" if rounded == 1 else "s")


def summary_line(data: Mapping[str, Any], timezone: str) -> str:
    """The digest in one plain line: what finished, what waits, what was
    decided, how long the lined-up work lasts — a part that is zero left
    out where that reads better. No names, no mentions."""
    zone = _zone(timezone)
    since, until = _instant(data.get("since")), _instant(data.get("until"))
    head = f"Since {_when(since, until, zone)}" if since and until else "Since the last digest"
    landed, failed = _count(data, "landed"), _count(data, "failed")
    finished = [f"{landed} landed"] if landed else []
    finished += [f"{failed} failed"] if failed else []
    parts = [", ".join(finished) or "nothing finished"]
    waiting = _count(data, "waiting")
    parts.append(f"{waiting} waiting on a person" if waiting else "nothing waiting on a person")
    allowed, escalated = _count(data, "decided_allow"), _count(data, "decided_escalate")
    decided = [f"{allowed} decided under grants"] if allowed else []
    decided += [f"{escalated} escalated to a person"] if escalated else []
    if decided:
        parts.append(", ".join(decided))
    runway = data.get("runway_days")
    if isinstance(runway, int | float) and not isinstance(runway, bool):
        parts.append(
            f"runway {_days(float(runway))}" if round(runway, 1) > 0 else "nothing lined up"
        )
    return f"{head}: {'; '.join(parts)}."


class Digest:
    """Sends the day's digest once, on the first tracker pass at or after
    ``[attention] digest_at``."""

    def __init__(self, ctx: ApiContext) -> None:
        self.ctx = ctx
        #: The local day already settled — sent, found sent, or tried.
        self._day: date | None = None

    def step(self, now: float) -> int:
        """One pass: how many events it recorded (0 or 1)."""
        config = self.ctx.config
        at = config.attention.digest_time
        if at is None:
            return 0
        timezone = config.daemon.run_cap_timezone
        local = datetime.fromtimestamp(now, tz=_zone(timezone))
        if local.time() < at or self._day == local.date():
            return 0
        day = local.date()
        last = self._last()
        if last.get("day") == day.isoformat():
            self._day = day
            return 0
        # Whatever happens next, this day is settled: a failure is logged
        # once, not retried every second.
        self._day = day
        return self._send(now, day, timezone, last)

    def _last(self) -> dict[str, Any]:
        raw = self.ctx.loop.dstore.get_value(STATE_KEY)
        try:
            kept = json.loads(raw) if raw else {}
        except ValueError:
            kept = {}
        return kept if isinstance(kept, dict) else {}

    def _send(self, now: float, day: date, timezone: str, last: Mapping[str, Any]) -> int:
        from lantern.api.briefing import WINDOW_S, briefing
        from lantern.api.projections import Views
        from lantern.api.usage import WINDOW_MAX_S

        previous = last.get("at")
        since = (
            float(previous)
            if isinstance(previous, int | float) and not isinstance(previous, bool)
            else now - WINDOW_S
        )
        if since >= now:
            since = now - WINDOW_S
        since = max(since, now - WINDOW_MAX_S)
        summary = briefing(Views(self.ctx), None, since=since)
        days = summary.runway.days
        data: dict[str, Any] = {
            "day": day.isoformat(),
            "since": summary.since,
            "until": summary.until,
            "landed": summary.outcomes.landed,
            "failed": summary.outcomes.failed,
            "waiting": summary.waiting.total,
            "decided_allow": summary.decided.allow,
            "decided_escalate": summary.decided.escalate,
            "runway_days": round(days, 2) if days is not None else None,
            "timezone": timezone,
        }
        # For everyone: no run, item or channel; the day kept with it.
        self.ctx.chronology.record(
            DIGEST,
            now,
            actor=DAEMON_ACTOR,
            data=data,
            state={STATE_KEY: json.dumps({"day": day.isoformat(), "at": now})},
        )
        text = summary_line(data, timezone)
        log.info(NOTICE, text=text, day=day.isoformat())
        frontend = getattr(self.ctx.loop, "frontend", None)
        if frontend is not None:
            try:
                frontend.daemon_notice(DaemonNotice(NOTICE, text, level="info"))
            except Exception:
                log.warning("frontend.daemon_notice_failed", notice=NOTICE, exc_info=True)
        return 1


__all__ = ["DIGEST", "NOTICE", "STATE_KEY", "Digest", "summary_line"]
