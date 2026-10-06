"""``attention.opened`` and ``attention.resolved``: the chronology says when
something starts waiting on a person and when it stops, so nothing has to
poll the list and diff it. The set of what was waiting is kept across a
restart, and an idle daemon pays nothing for it."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert

from lantern.api.attention_events import OPEN_PREFIX, SWEEP_S, AttentionTracker
from lantern.api.projector import Projector
from lantern.daemon.sources import RepoHealth
from lantern.db.api_models import ApiEventRow
from tests.api.conftest import Api
from tests.api.test_attention import SuspendedSource, _blocked, _entries, _parked
from tests.api.test_channel_access import _channel, _invite
from tests.api.test_collaboration import bearer, register
from tests.api.test_control import gated, landed
from tests.api.test_read_path_cost import statements, touching
from tests.unit.test_daemon_loop import gh_item

#: Tables the list is computed from: a pass that reads none of them
#: recomputed nothing.
LIST_TABLES = ("work_items", "merge_gates", "epic_runs", "provider_holds")


def _step(api: Api, tracker: AttentionTracker | None = None) -> int:
    """One pass of the tracker, as the projector's step makes it: handed
    the time and the chronology's high-water mark."""
    tracker = tracker or api.ctx.attention
    return int(tracker.step(api.clock(), api.ctx.chronology.watermark()))


def _events(api: Api, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    page = api.client.get(
        "/v1/events",
        params={"type_prefix": "attention.", "limit": 200},
        headers=headers or api.bearer(),
    )
    assert page.status_code == 200, page.text
    return list(page.json()["data"])


def _told(api: Api, headers: dict[str, str] | None = None) -> list[tuple[str, str]]:
    return [(e["type"], e["data"]["entry_id"]) for e in _events(api, headers)]


def _recomputed(seen: list[str]) -> bool:
    return any(touching(seen, table) for table in LIST_TABLES)


class TestOpenedAndResolved:
    def test_a_new_entry_is_announced_with_what_the_list_says_of_it(self, api: Api) -> None:
        assert _step(api) == 0
        _blocked(api)
        (entry,) = _entries(api)
        assert _step(api) == 1
        (event,) = _events(api)
        assert event["type"] == "attention.opened"
        # Scoped as the run's own events are: by its run and its item.
        assert event["run_id"] == entry["run_id"] and event["item_id"] == entry["item_id"]
        assert event["actor"]["kind"] == "system"
        assert event["data"] == {
            "entry_id": entry["id"],
            "kind": "item",
            "group": "failed",
            "state": "blocked",
            "title": "Do 1",
            "since": entry["since"],
            "repository": entry["repository"],
            "repository_id": entry["repository_id"],
            "item_id": entry["item_id"],
            "run_id": entry["run_id"],
            "gate_id": None,
            "plan_id": None,
            "node_id": None,
            "epic_run_id": None,
            "revision": entry["revision"],
        }
        # Announced once: the same thing still waiting is not news.
        api.clock.t += SWEEP_S
        assert _step(api) == 0 and len(_events(api)) == 1

    def test_a_retried_item_is_resolved(self, api: Api) -> None:
        _step(api)
        _blocked(api)
        (entry,) = _entries(api)
        _step(api)
        retried = api.client.post(f"/v1/items/{entry['item_id']}/retry", headers=api.bearer())
        assert retried.status_code == 200, retried.text
        assert _step(api) == 1
        opened, resolved = _events(api)
        assert resolved["type"] == "attention.resolved"
        # The same entry, as it was announced, though it is no longer listed.
        assert resolved["data"] == opened["data"]
        assert resolved["run_id"] == opened["run_id"] and resolved["item_id"] == opened["item_id"]
        assert _step(api) == 0

    def test_an_approved_gate_is_resolved(self, api: Api) -> None:
        _step(api)
        run_id = gated(api)
        headers = api.bearer()
        (entry,) = _entries(api, headers)
        assert _step(api) == 1
        assert _told(api) == [("attention.opened", entry["id"])]
        assert _events(api)[0]["data"]["gate_id"] == entry["gate_id"]
        approved = api.client.post(
            f"/v1/gates/{entry['gate_id']}/approve",
            json={"expected_revision": entry["revision"]},
            headers=headers,
        )
        assert approved.status_code == 202, approved.text
        landed(api, run_id)
        _step(api)
        assert _told(api) == [
            ("attention.opened", entry["id"]),
            ("attention.resolved", entry["id"]),
        ]

    def test_a_dismissed_alert_is_resolved_and_opens_again_when_it_is_taken_back(
        self, api: Api
    ) -> None:
        _step(api)
        _blocked(api)
        (entry,) = _entries(api)
        _step(api)
        headers = api.bearer()
        assert (
            api.client.post(f"/v1/items/{entry['item_id']}/dismiss", headers=headers).status_code
            == 200
        )
        assert _step(api) == 1
        assert (
            api.client.post(f"/v1/items/{entry['item_id']}/undismiss", headers=headers).status_code
            == 200
        )
        assert _step(api) == 1
        assert [kind for kind, _ in _told(api)] == [
            "attention.opened",
            "attention.resolved",
            "attention.opened",
        ]
        assert {entry_id for _, entry_id in _told(api)} == {entry["id"]}

    def test_work_that_fails_again_is_a_new_entry(self, api: Api) -> None:
        _step(api)
        _blocked(api)
        (first,) = _entries(api)
        _step(api)
        api.client.post(f"/v1/items/{first['item_id']}/retry", headers=api.bearer())
        api.harness.outcomes = ["blocked"]
        api.clock.t += 10
        assert api.loop.tick().outcome == "blocked"
        (second,) = _entries(api)
        # One pass saw both: the old alert gone, the new one raised.
        assert _step(api) == 2
        assert sorted(_told(api)[1:]) == sorted(
            [("attention.resolved", first["id"]), ("attention.opened", second["id"])]
        )


class TestTheFirstRun:
    def test_what_was_already_waiting_is_recorded_without_a_word(self, api: Api) -> None:
        """An upgrade must not announce everything that was waiting before
        it as new."""
        _blocked(api)
        _parked(api, "2")
        entries = _entries(api)
        assert len(entries) == 2
        assert _step(api) == 0 and _events(api) == []
        kept = api.harness.dstore.values_with_prefix(OPEN_PREFIX)
        assert sorted(kept) == sorted(OPEN_PREFIX + entry["id"] for entry in entries)
        # Remembered all the same: its leaving is told.
        first = entries[0]
        api.client.post(f"/v1/items/{first['item_id']}/retry", headers=api.bearer())
        assert _step(api) == 1
        assert _told(api) == [("attention.resolved", first["id"])]

    def test_an_empty_first_run_is_still_a_first_run(self, api: Api) -> None:
        assert _step(api) == 0
        _blocked(api)
        # Seeded with nothing, so the next thing to wait is news.
        assert _step(api) == 1


class TestARestart:
    def test_nothing_open_is_announced_again_and_no_resolution_is_lost(self, api: Api) -> None:
        _step(api)
        _blocked(api)
        (entry,) = _entries(api)
        _step(api)
        assert _told(api) == [("attention.opened", entry["id"])]
        # A new process: the set it left is what it starts from.
        assert _step(api, AttentionTracker(api.ctx)) == 0
        assert _told(api) == [("attention.opened", entry["id"])]
        # What settled while nothing was watching is told by the next one.
        api.client.post(f"/v1/items/{entry['item_id']}/retry", headers=api.bearer())
        assert _step(api, AttentionTracker(api.ctx)) == 1
        assert _step(api, AttentionTracker(api.ctx)) == 0
        assert _told(api) == [
            ("attention.opened", entry["id"]),
            ("attention.resolved", entry["id"]),
        ]

    def test_nothing_is_tracked_until_the_daemon_is_ready(self, api: Api) -> None:
        _blocked(api)
        api.ctx.ready.clear()
        mark = api.ctx.chronology.watermark()
        with statements(api) as seen:
            assert api.ctx.attention.step(api.clock(), mark) == 0
        assert seen == []
        api.ctx.ready.set()
        assert _step(api) == 0
        assert api.harness.dstore.values_with_prefix(OPEN_PREFIX)


class TestWhatItCosts:
    def _projector(self, api: Api, *, tracking: bool) -> Projector:
        projector = Projector(api.ctx.chronology, api.ctx.hub, clock=api.clock, retention_s=3600.0)
        if tracking:
            projector.attend_with(api.ctx.attention.step)
        return projector

    def test_an_idle_step_recomputes_nothing(self, api: Api) -> None:
        """The projector steps every second. With nothing recorded since
        the last pass the tracker runs no statement at all: the step costs
        what it cost before there was a tracker."""
        _blocked(api)
        bare = self._projector(api, tracking=False)
        tracked = self._projector(api, tracking=True)
        bare.step()
        tracked.step()
        api.clock.t += 1
        with statements(api) as seen:
            bare.step()
            without = len(seen)
        api.clock.t += 1
        with statements(api) as seen:
            assert tracked.step() == 0
            assert not _recomputed(seen)
            assert len(seen) == without, seen

    def test_only_what_can_change_the_list_recomputes_it(self, api: Api) -> None:
        _step(api)
        dstore = api.harness.dstore
        # A run's own output, projected from the engine, and a chat's
        # traffic: neither is a reason to read the list again.
        with dstore.transaction() as session:
            session.execute(
                insert(ApiEventRow).values(
                    recorded_at=api.clock(),
                    occurred_at=api.clock(),
                    type="run.note",
                    run_id="r1",
                    source_seq=1,
                )
            )
        api.ctx.chronology.record("collaboration.read_state", api.clock(), data={})
        with statements(api) as seen:
            assert _step(api) == 0
        # The mark, and whether anything past it could matter: no more.
        assert not _recomputed(seen), seen
        assert [sql for sql in seen if sql != "BEGIN"] == [
            sql for sql in seen if "FROM api_events" in sql
        ]
        assert touching(seen, "FROM api_events") == 2, seen
        # A notice from the daemon is.
        api.ctx.chronology.record("daemon.notice", api.clock(), data={"kind": "x"})
        with statements(api) as seen:
            assert _step(api) == 0
        assert _recomputed(seen)

    def test_its_own_events_do_not_wake_it(self, api: Api) -> None:
        _step(api)
        _blocked(api)
        assert _step(api) == 1
        with statements(api) as seen:
            assert _step(api) == 0
        assert not _recomputed(seen)

    def test_a_change_that_records_nothing_is_found_by_the_sweep(self, api: Api) -> None:
        _step(api)
        # Polling stops without a public event of its own.
        api.loop.source = SuspendedSource(RepoHealth("o/r", 4, None, True, "gone", 1.0))
        (entry,) = _entries(api)
        assert _step(api) == 0
        api.clock.t += SWEEP_S
        assert _step(api) == 1
        (event,) = _events(api)
        assert event["type"] == "attention.opened" and event["data"]["entry_id"] == entry["id"]
        assert event["data"]["kind"] == "repository" and event["data"]["state"] == "suspended"
        assert event["run_id"] is None and event["item_id"] is None

    def test_the_projector_tells_the_streams_what_it_announced(self, api: Api) -> None:
        projector = self._projector(api, tracking=True)
        projector.step()
        woken: list[int] = []
        projector.listen(lambda: woken.append(1))
        _blocked(api)
        projector.step()
        assert len(_events(api)) == 1 and woken
        # The announcement is already seen: the next step wakes nobody.
        woken.clear()
        projector.step()
        assert woken == []


class TestWhoSeesThem:
    def test_the_events_follow_the_work_they_are_about(self, api: Api) -> None:
        owner = register(api)
        guest = _invite(api, "member", "guest")
        admin = _invite(api, "admin", "admin")
        shared = _channel(api, bearer(owner), "workspace")
        private = _channel(api, bearer(owner))
        _step(api)
        dstore = api.harness.dstore
        for key, channel in (("1", None), ("2", shared), ("3", private)):
            api.clock.t += 10
            fields = {} if channel is None else {"channel_id": channel}
            dstore.upsert_new(gh_item(key, **fields), api.clock())
            dstore.mark_blocked(f"gh:{key}", "stuck", api.clock())
        api.loop.source = SuspendedSource(RepoHealth("o/r", 4, None, True, "gone", 1.0))
        api.clock.t += SWEEP_S
        assert _step(api) == 4
        by_title = {e["title"]: e["id"] for e in _entries(api)}
        nobody, in_shared, in_private = by_title["Do 1"], by_title["Do 2"], by_title["Do 3"]
        (repository,) = [e["id"] for e in _entries(api) if e["kind"] == "repository"]

        def seen(token: dict[str, Any] | None) -> set[str]:
            return {entry_id for _, entry_id in _told(api, bearer(token) if token else None)}

        # A plain API client sees every event.
        assert seen(None) == {nobody, in_shared, in_private, repository}
        # Work a channel asked for is its readers'; work no channel asked
        # for is the owners' and admins'; a block on the daemon itself has
        # no run and no item, and every member is told.
        assert seen(owner) == {nobody, in_shared, in_private, repository}
        assert seen(admin) == {nobody, in_shared, repository}
        assert seen(guest) == {in_shared, repository}
        # The resolution is scoped as the opening was.
        for key in ("1", "2", "3"):
            item = next(i for i in dstore.items() if i.source_key == key)
            dstore.mark_cancelled(item.item_id, "stopped", api.clock())
        api.clock.t += SWEEP_S
        assert _step(api) == 3
        resolved = {
            e["data"]["entry_id"]
            for e in _events(api, bearer(guest))
            if e["type"] == "attention.resolved"
        }
        assert resolved == {in_shared}
