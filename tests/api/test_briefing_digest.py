"""The daily digest: a person who stops watching still hears, once a day,
what happened — without asking. At ``[attention] digest_at`` (a local time
in ``[daemon] run_cap_timezone``) the attention tracker records one
``briefing.digest`` event with the briefing's numbers since the previous
digest, says the same in one control-channel line, and the push rules turn
it into one ``work`` push per member.

Once a day and never a burst: the day of the last digest is kept in
``daemon_state``, so a restart repeats nothing, a daemon that was down at
the digest time sends it once when it returns the same day, and a day it
missed entirely is skipped rather than made up."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lantern.api.attention_events import AttentionTracker
from lantern.api.digest import DIGEST, STATE_KEY, summary_line
from lantern.api.frontend import ApiFrontend
from lantern.api.models import rfc3339
from lantern.api.projector import Projector
from lantern.config import Config
from lantern.daemon.fanout import FanoutFrontend
from tests.api.conftest import Api, build
from tests.api.test_attention import _blocked
from tests.api.test_attention_events import _step
from tests.api.test_briefing import _decide, _finished
from tests.api.test_grants import _headers
from tests.api.test_read_path_cost import statements
from tests.api.test_role_grants import _register_member, _register_owner
from tests.unit.test_daemon_loop import RecordingFrontend

DAY = 86400.0
#: Midnight UTC on a day; the digest is due at 07:00 on it.
MIDNIGHT = datetime(2026, 10, 2, tzinfo=UTC).timestamp()
AT = MIDNIGHT + 7 * 3600


def _digest_api(tmp_path: Path, digest_at: str = "07:00", **daemon: Any) -> Api:
    config: dict[str, Any] = {"attention": {"digest_at": digest_at}}
    if daemon:
        config["daemon"] = daemon
    api = build(tmp_path, config=config)
    api.clock.t = AT - 3600
    return api


@pytest.fixture
def digest(tmp_path: Path) -> Any:
    api = _digest_api(tmp_path)
    with api.client:
        yield api
    api.ctx.close()


def _digests(api: Api, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    page = api.client.get(
        "/v1/events",
        params={"type_prefix": "briefing.", "limit": 200},
        headers=headers or api.bearer(),
    )
    assert page.status_code == 200, page.text
    return [e for e in page.json()["data"] if e["type"] == DIGEST]


def _at(api: Api, t: float, tracker: AttentionTracker | None = None) -> int:
    api.clock.t = t
    return _step(api, tracker)


class TestOff:
    def test_off_by_default(self, api: Api) -> None:
        assert api.ctx.config.attention.digest_at == ""
        for t in (AT - 60, AT, AT + 3600, AT + DAY, AT + 2 * DAY + 60):
            _at(api, t)
        assert _digests(api) == []
        assert api.harness.dstore.get_value(STATE_KEY) is None

    def test_an_idle_daemon_with_the_digest_off_does_no_extra_work(self, api: Api) -> None:
        bare = Projector(api.ctx.chronology, api.ctx.hub, clock=api.clock, retention_s=3600.0)
        tracked = Projector(api.ctx.chronology, api.ctx.hub, clock=api.clock, retention_s=3600.0)
        tracked.attend_with(api.ctx.attention.step)
        api.clock.t = AT + 60
        bare.step()
        tracked.step()
        api.clock.t += 1
        with statements(api) as seen:
            bare.step()
            without = len(seen)
        api.clock.t += 1
        with statements(api) as seen:
            assert tracked.step() == 0
            assert len(seen) == without, seen


class TestWhen:
    def test_not_before_the_time(self, digest: Api) -> None:
        assert _at(digest, AT - 60) == 0
        assert _digests(digest) == []

    def test_once_at_the_time_and_not_again_that_day(self, digest: Api) -> None:
        _at(digest, AT - 60)
        assert _at(digest, AT) == 1
        assert len(_digests(digest)) == 1
        for t in (AT + 60, AT + 3600, MIDNIGHT + DAY - 1):
            assert _at(digest, t) == 0
        assert len(_digests(digest)) == 1

    def test_the_next_one_is_the_next_day_at_the_time(self, digest: Api) -> None:
        _at(digest, AT)
        assert _at(digest, MIDNIGHT + DAY + 60) == 0
        assert _at(digest, AT + DAY - 60) == 0
        assert _at(digest, AT + DAY) == 1
        assert [d["data"]["day"] for d in _digests(digest)] == ["2026-10-02", "2026-10-03"]

    def test_a_restart_does_not_send_it_twice(self, digest: Api) -> None:
        _at(digest, AT)
        assert _at(digest, AT + 120, AttentionTracker(digest.ctx)) == 0
        assert _at(digest, AT + 7200, AttentionTracker(digest.ctx)) == 0
        assert len(_digests(digest)) == 1

    def test_a_daemon_down_at_the_time_sends_it_once_on_return_the_same_day(
        self, digest: Api
    ) -> None:
        _at(digest, AT - DAY)  # yesterday's
        assert len(_digests(digest)) == 1
        # Down from before 07:00 to the afternoon: one, on return.
        assert _at(digest, AT + 6 * 3600, AttentionTracker(digest.ctx)) == 1
        assert _at(digest, AT + 6 * 3600 + 60) == 0
        assert len(_digests(digest)) == 2

    def test_a_missed_day_is_skipped_not_made_up(self, digest: Api) -> None:
        _at(digest, AT)
        # Down for all of the next day; back the morning after, late.
        assert _at(digest, AT + 2 * DAY + 3600, AttentionTracker(digest.ctx)) == 1
        assert _at(digest, AT + 2 * DAY + 3660) == 0
        days = [d["data"]["day"] for d in _digests(digest)]
        assert days == ["2026-10-02", "2026-10-04"]
        # It still covers everything since the previous one.
        assert _digests(digest)[-1]["data"]["since"] == rfc3339(AT)

    def test_the_time_is_read_in_the_run_cap_timezone(self, tmp_path: Path) -> None:
        api = _digest_api(tmp_path, "07:00", run_cap_timezone="America/New_York")
        with api.client:
            # 07:00 UTC is 03:00 in New York (daylight time): not yet.
            assert _at(api, AT) == 0
            assert _at(api, AT + 4 * 3600 - 60) == 0
            assert _at(api, AT + 4 * 3600) == 1
            (event,) = _digests(api)
            assert event["data"]["day"] == "2026-10-02"
        api.ctx.close()

    def test_nothing_before_the_daemon_is_ready(self, tmp_path: Path) -> None:
        api = build(tmp_path, ready=False, config={"attention": {"digest_at": "07:00"}})
        with api.client:
            assert _at(api, AT + 60) == 0
            assert api.harness.dstore.get_value(STATE_KEY) is None
        api.ctx.close()

    def test_once_sent_a_pass_runs_no_statement(self, digest: Api) -> None:
        _at(digest, AT)
        _at(digest, AT + 1)
        digest.clock.t = AT + 2
        with statements(digest) as seen:
            assert digest.ctx.attention.step(digest.clock(), digest.ctx.chronology.watermark()) == 0
        assert not any("daemon_state" in sql for sql in seen), seen


class TestWhatItSays:
    def test_the_event_carries_the_numbers_and_nothing_else(self, digest: Api) -> None:
        _blocked(digest)
        _decide(digest, "allow", at=AT - 600, grant_id="grant_a")
        _decide(digest, "allow", at=AT - 500, grant_id="grant_a")
        _decide(digest, "escalate", at=AT - 400)
        _decide(digest, "deny", at=AT - 300)
        _at(digest, AT)
        (event,) = _digests(digest)
        assert event["data"] == {
            "day": "2026-10-02",
            "since": rfc3339(AT - DAY),
            "until": rfc3339(AT),
            "landed": 0,
            "failed": 0,
            "waiting": 1,
            "decided_allow": 2,
            "decided_escalate": 1,
            "runway_days": None,
            "timezone": "UTC",
        }
        # For everyone: no run, no item, no channel.
        assert event["run_id"] is None and event["item_id"] is None
        assert event.get("channel_id") is None
        assert event["actor"]["kind"] == "system"

    def test_every_member_can_read_it(self, digest: Api) -> None:
        _register_owner(digest)
        _at(digest, AT)
        member = _headers(_register_member(digest))
        (event,) = _digests(digest, member)
        assert event["data"]["day"] == "2026-10-02"

    def test_it_covers_the_time_since_the_previous_digest(self, digest: Api) -> None:
        _finished(digest, "1", "merged", ended_at=AT - 3600)
        _at(digest, AT)
        _finished(digest, "2", "merged", ended_at=AT + 3600)
        _finished(digest, "3", "failed", ended_at=AT + 7200)
        _at(digest, AT + DAY)
        first, second = _digests(digest)
        assert (first["data"]["landed"], first["data"]["failed"]) == (1, 0)
        assert second["data"]["since"] == rfc3339(AT)
        assert second["data"]["until"] == rfc3339(AT + DAY)
        assert (second["data"]["landed"], second["data"]["failed"]) == (1, 1)

    def test_one_control_channel_line_through_the_frontend(self, digest: Api) -> None:
        recording = RecordingFrontend()
        fanout = FanoutFrontend([])
        fanout.add_observer(recording)
        fanout.add_observer(
            ApiFrontend(digest.ctx.chronology, digest.ctx.hub, None, clock=digest.clock)
        )
        digest.loop.frontend = fanout
        _finished(digest, "1", "merged", ended_at=AT - 3600)
        _finished(digest, "2", "failed", ended_at=AT - 1800)
        _decide(digest, "allow", at=AT - 600, grant_id="grant_a")
        _at(digest, AT)
        _at(digest, AT + 60)
        notices = [n for n in recording.notices if n.kind == "daemon.digest"]
        (notice,) = notices
        assert notice.level == "info" and notice.mention_ids == ()
        assert notice.run_id is None and notice.item_id is None
        assert notice.text == (
            "Since yesterday 07:00: 1 landed, 1 failed; "
            "nothing waiting on a person; 1 decided under grants; nothing lined up."
        )
        # The same line is in the chronology, as every notice is.
        page = digest.client.get(
            "/v1/events", params={"type_prefix": "daemon.notice"}, headers=digest.bearer()
        ).json()["data"]
        assert [e["data"]["text"] for e in page if e["data"]["kind"] == "daemon.digest"] == [
            notice.text
        ]

    def test_a_daemon_without_a_frontend_still_records_the_event(self, digest: Api) -> None:
        digest.loop.frontend = None
        assert _at(digest, AT) == 1
        assert len(_digests(digest)) == 1

    def test_the_state_kept_names_the_day_and_the_time(self, digest: Api) -> None:
        _at(digest, AT + 5)
        kept = json.loads(digest.harness.dstore.get_value(STATE_KEY) or "")
        assert kept == {"day": "2026-10-02", "at": AT + 5}


class TestTheLine:
    def _line(self, **numbers: Any) -> str:
        data = {
            "since": rfc3339(AT - DAY),
            "until": rfc3339(AT),
            "landed": 0,
            "failed": 0,
            "waiting": 0,
            "decided_allow": 0,
            "decided_escalate": 0,
            "runway_days": None,
            **numbers,
        }
        return summary_line(data, "UTC")

    def test_the_full_line(self) -> None:
        assert self._line(
            landed=11, failed=1, waiting=2, decided_allow=9, decided_escalate=1, runway_days=2.46
        ) == (
            "Since yesterday 07:00: 11 landed, 1 failed; 2 waiting on a person; "
            "9 decided under grants, 1 escalated to a person; runway 2.5 days."
        )

    def test_zero_parts_are_left_out(self) -> None:
        assert (
            self._line() == "Since yesterday 07:00: nothing finished; nothing waiting on a person."
        )
        assert self._line(failed=2, runway_days=1.0) == (
            "Since yesterday 07:00: 2 failed; nothing waiting on a person; runway 1 day."
        )
        assert self._line(landed=3, runway_days=4.0) == (
            "Since yesterday 07:00: 3 landed; nothing waiting on a person; runway 4 days."
        )
        # Landings and nothing ready to start: said, not "runway 0 days".
        assert self._line(landed=3, runway_days=0.0) == (
            "Since yesterday 07:00: 3 landed; nothing waiting on a person; nothing lined up."
        )

    def test_since_is_said_as_a_person_would(self) -> None:
        same_day = self._line(since=rfc3339(AT - 3600))
        assert same_day.startswith("Since today 06:00:")
        long_ago = self._line(since=rfc3339(AT - 3 * DAY))
        assert long_ago.startswith("Since Tue 29 Sep 07:00:")

    def test_since_is_said_in_the_timezone(self) -> None:
        data = {
            "since": rfc3339(AT - DAY),
            "until": rfc3339(AT),
            "landed": 1,
        }
        assert summary_line(data, "America/New_York").startswith("Since yesterday 03:00:")


class TestTheKnob:
    @pytest.mark.parametrize("value", ["07:00", "00:00", "23:59", "7:05", ""])
    def test_a_time_of_day_or_empty_loads(self, value: str) -> None:
        config = Config.model_validate({"attention": {"digest_at": value}})
        assert config.attention.digest_at == value
        assert (config.attention.digest_time is None) == (value == "")

    @pytest.mark.parametrize("value", ["24:00", "07:60", "7am", "07", "07:00:00", " ", "-1:00"])
    def test_anything_else_is_refused_naming_the_key(self, value: str) -> None:
        with pytest.raises(ValidationError) as caught:
            Config.model_validate({"attention": {"digest_at": value}})
        message = str(caught.value)
        assert "attention.digest_at" in message and "HH:MM" in message
