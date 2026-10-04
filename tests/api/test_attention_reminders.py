"""``attention.reminder``: a thing that waits on a person is not left to
wait in silence. After ``[attention] remind_after_s`` the tracker records
one reminder for it, and another every ``remind_every_s`` — never a burst
after a long stop, never twice for the same interval across a restart,
and nothing at all for what was dismissed, resolved, or is not yet old
enough."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lantern.api.attention_events import OPEN_PREFIX, SEEDED_KEY, SWEEP_S, AttentionTracker
from lantern.api.projector import Projector
from lantern.config import Config
from tests.api.conftest import Api, build
from tests.api.test_attention import _blocked, _entries
from tests.api.test_attention_events import _events, _step
from tests.api.test_read_path_cost import statements

AFTER = Config().attention.remind_after_s
EVERY = Config().attention.remind_every_s


def _reminders(api: Api) -> list[dict[str, Any]]:
    return [e for e in _events(api) if e["type"] == "attention.reminder"]


def _opened(api: Api) -> dict[str, Any]:
    (event,) = [e for e in _events(api) if e["type"] == "attention.opened"]
    return event


def _wait(api: Api, seconds: float) -> int:
    """Let ``seconds`` (a sweep at least) go by and sweep once; returns
    how many events the sweep recorded."""
    assert seconds >= SWEEP_S
    api.clock.t += seconds
    return _step(api)


def _open_one(api: Api) -> dict[str, Any]:
    """One blocked item, announced; the entry as listed."""
    assert _step(api) == 0
    _blocked(api)
    (entry,) = _entries(api)
    assert _step(api) == 1
    return dict(entry)


class TestWhenAReminderIsSent:
    def test_not_before_remind_after_s(self, api: Api) -> None:
        _open_one(api)
        assert _wait(api, AFTER - SWEEP_S) == 0
        assert _reminders(api) == []

    def test_once_after_it_with_what_the_opening_said_and_how_long_it_waited(
        self, api: Api
    ) -> None:
        entry = _open_one(api)
        assert _wait(api, AFTER) == 1
        (reminder,) = _reminders(api)
        opened = _opened(api)
        assert reminder["data"] == {
            **opened["data"],
            "waiting_s": AFTER,
            "reminders": 1,
            "capabilities": sorted({a["capability"] for a in entry["actions"]}),
        }
        assert reminder["data"]["capabilities"] == ["runs:control"]
        # Scoped as the opening was: the work's own run and item.
        assert reminder["run_id"] == opened["run_id"] and reminder["item_id"] == opened["item_id"]
        assert reminder["actor"]["kind"] == "system"
        # Still waiting a minute later is not news again.
        assert _wait(api, SWEEP_S) == 0
        assert len(_reminders(api)) == 1

    def test_again_only_after_remind_every_s_and_the_count_goes_up(self, api: Api) -> None:
        _open_one(api)
        _wait(api, AFTER)
        assert _wait(api, EVERY - SWEEP_S) == 0
        assert _wait(api, SWEEP_S) == 1
        assert _wait(api, EVERY) == 1
        assert [r["data"]["reminders"] for r in _reminders(api)] == [1, 2, 3]
        assert [r["data"]["waiting_s"] for r in _reminders(api)] == [
            AFTER,
            AFTER + EVERY,
            AFTER + 2 * EVERY,
        ]

    def test_a_long_stop_is_one_reminder_on_return_not_a_burst(self, api: Api) -> None:
        _open_one(api)
        _wait(api, AFTER)
        # Down for a week: whatever was missed is one reminder, now.
        api.clock.t += 5 * EVERY
        assert _step(api, AttentionTracker(api.ctx)) == 1
        assert [r["data"]["reminders"] for r in _reminders(api)] == [1, 2]
        assert _reminders(api)[-1]["data"]["waiting_s"] == AFTER + 5 * EVERY
        # And the clock runs from that one.
        api.clock.t += EVERY - SWEEP_S
        assert _step(api) == 0
        api.clock.t += SWEEP_S
        assert _step(api) == 1

    def test_a_restart_neither_repeats_a_reminder_nor_resets_the_clock(self, api: Api) -> None:
        _open_one(api)
        _wait(api, AFTER)
        assert len(_reminders(api)) == 1
        # A new process right after: the reminder it sent is not sent again.
        api.clock.t += SWEEP_S
        assert _step(api, AttentionTracker(api.ctx)) == 0
        assert len(_reminders(api)) == 1
        # Another new process halfway to the next: the wait so far counts.
        api.clock.t += EVERY / 2
        assert _step(api, AttentionTracker(api.ctx)) == 0
        api.clock.t += EVERY / 2
        assert _step(api, AttentionTracker(api.ctx)) == 1
        assert [r["data"]["reminders"] for r in _reminders(api)] == [1, 2]

    def test_a_restart_before_the_first_counts_the_wait_so_far(self, api: Api) -> None:
        _open_one(api)
        api.clock.t += AFTER / 2
        assert _step(api, AttentionTracker(api.ctx)) == 0
        api.clock.t += AFTER / 2
        assert _step(api, AttentionTracker(api.ctx)) == 1
        assert len(_reminders(api)) == 1


class TestWhatGetsNone:
    def test_nothing_with_remind_after_s_at_zero(self, tmp_path: Path) -> None:
        api = build(tmp_path, config={"attention": {"remind_after_s": 0}})
        with api.client:
            _open_one(api)
            assert _wait(api, 2 * AFTER + EVERY) == 0
            assert _reminders(api) == []
        api.ctx.close()

    def test_a_dismissed_entry_is_off_the_list_and_gets_none(self, api: Api) -> None:
        entry = _open_one(api)
        dismissed = api.client.post(f"/v1/items/{entry['item_id']}/dismiss", headers=api.bearer())
        assert dismissed.status_code == 200, dismissed.text
        assert _step(api) == 1  # resolved
        assert _wait(api, AFTER + EVERY) == 0
        assert _reminders(api) == []

    def test_a_resolved_entry_gets_none(self, api: Api) -> None:
        entry = _open_one(api)
        _wait(api, AFTER)
        retried = api.client.post(f"/v1/items/{entry['item_id']}/retry", headers=api.bearer())
        assert retried.status_code == 200, retried.text
        assert _step(api) == 1  # resolved
        assert _wait(api, 2 * EVERY) == 0
        assert len(_reminders(api)) == 1

    def test_an_entry_that_returns_under_a_new_id_starts_fresh(self, api: Api) -> None:
        first = _open_one(api)
        _wait(api, AFTER)
        api.client.post(f"/v1/items/{first['item_id']}/retry", headers=api.bearer())
        api.harness.outcomes = ["blocked"]
        api.clock.t += 10
        assert api.loop.tick().outcome == "blocked"
        (second,) = _entries(api)
        assert second["id"] != first["id"]
        assert _step(api) == 2  # the old resolved, the new opened
        # Not overdue because the old one was: its own clock starts now.
        assert _wait(api, AFTER - SWEEP_S) == 0
        assert _wait(api, SWEEP_S) == 1
        reminders = _reminders(api)
        assert [r["data"]["entry_id"] for r in reminders] == [first["id"], second["id"]]
        assert reminders[-1]["data"]["reminders"] == 1

    def test_an_entry_taken_back_from_dismissal_starts_fresh(self, api: Api) -> None:
        entry = _open_one(api)
        _wait(api, AFTER)
        api.client.post(f"/v1/items/{entry['item_id']}/dismiss", headers=api.bearer())
        _step(api)
        api.clock.t += EVERY
        taken_back = api.client.post(
            f"/v1/items/{entry['item_id']}/undismiss", headers=api.bearer()
        )
        assert taken_back.status_code == 200, taken_back.text
        assert _step(api) == 1  # opened again, no reminder with it
        assert _wait(api, AFTER - SWEEP_S) == 0
        assert _wait(api, SWEEP_S) == 1
        assert [r["data"]["reminders"] for r in _reminders(api)] == [1, 1]


class TestWhatItCosts:
    def test_an_idle_daemon_does_no_work(self, api: Api) -> None:
        """Nothing open, the reminder window long past: the projector's
        idle pass still runs no statement, and the sweep writes nothing."""
        bare = Projector(api.ctx.chronology, api.ctx.hub, clock=api.clock, retention_s=3600.0)
        tracked = Projector(api.ctx.chronology, api.ctx.hub, clock=api.clock, retention_s=3600.0)
        tracked.attend_with(api.ctx.attention.step)
        api.clock.t += AFTER + EVERY
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
        api.clock.t += SWEEP_S
        with statements(api) as seen:
            tracked.step()
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in seen), seen

    def test_a_sweep_with_nothing_due_writes_nothing(self, api: Api) -> None:
        _open_one(api)
        api.clock.t += SWEEP_S
        with statements(api) as seen:
            assert _step(api) == 0
        assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in seen), seen


class TestWhatThePreviousReleaseWrote:
    def _as_before(self, api: Api, entry_id: str) -> None:
        """The value the release before reminders kept for an open entry:
        the record with no timestamps."""
        raw = api.harness.dstore.get_value(OPEN_PREFIX + entry_id)
        assert raw is not None
        record = json.loads(raw)
        legacy = {k: record[k] for k in ("run_id", "item_id", "data")}
        api.harness.dstore.set_value(OPEN_PREFIX + entry_id, json.dumps(legacy))

    def test_an_entry_kept_without_timestamps_is_first_seen_now_never_overdue(
        self, api: Api
    ) -> None:
        entry = _open_one(api)
        api.clock.t += 3 * AFTER
        self._as_before(api, entry["id"])
        # The first pass of the new release stamps it and asks nothing.
        assert _step(api, AttentionTracker(api.ctx)) == 0
        assert _reminders(api) == []
        assert _wait(api, AFTER - SWEEP_S) == 0
        assert _wait(api, SWEEP_S) == 1
        (reminder,) = _reminders(api)
        assert reminder["data"]["waiting_s"] == AFTER and reminder["data"]["reminders"] == 1

    def test_an_unreadable_value_is_read_without_error(self, api: Api) -> None:
        entry = _open_one(api)
        api.harness.dstore.set_value(OPEN_PREFIX + entry["id"], "not json")
        assert api.harness.dstore.get_value(SEEDED_KEY) is not None
        api.clock.t += AFTER
        assert _step(api, AttentionTracker(api.ctx)) == 0
        assert _wait(api, AFTER) == 1
        (reminder,) = _reminders(api)
        assert reminder["data"]["entry_id"] == entry["id"]
        # The resolution still names the entry.
        api.client.post(f"/v1/items/{entry['item_id']}/retry", headers=api.bearer())
        assert _step(api) == 1
        assert _events(api)[-1]["type"] == "attention.resolved"

    def test_the_record_keeps_what_it_kept_and_adds_the_clock(self, api: Api) -> None:
        entry = _open_one(api)
        record = json.loads(api.harness.dstore.get_value(OPEN_PREFIX + entry["id"]) or "")
        assert set(record) == {"run_id", "item_id", "data", "opened_at", "reminded_at", "reminders"}
        assert record["opened_at"] == api.clock() and record["reminded_at"] is None
        assert record["reminders"] == 0
        _wait(api, AFTER)
        record = json.loads(api.harness.dstore.get_value(OPEN_PREFIX + entry["id"]) or "")
        assert record["reminded_at"] == api.clock() and record["reminders"] == 1
        assert record["opened_at"] == api.clock() - AFTER
