"""A mark on work stands until the work moves again.

A dismissal acknowledges the state a person looked at. These hold the rule
that takes it away — a trigger, so it holds for a write from any process —
and the ones that must *not*: a write that leaves the state where it was,
and a run settling under an item that already carries the mark.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from lantern.daemon.store import TERMINAL_ITEM_STATES, DaemonStore
from lantern.db.schema import MIGRATIONS_DIR
from lantern.db.work_marks import RESTING_ITEM_STATES, TRIGGERS
from lantern.engine.store import StateStore
from lantern.paths import LanternHome
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to
from tests.unit.test_daemon_loop import gh_item

ACTOR = {"kind": "operator", "id": "usr_1", "display": "Sam", "via": "api"}


@pytest.fixture
def dstore(tmp_path: Path) -> DaemonStore:
    return DaemonStore(LanternHome(tmp_path).state_db)


def _admitted(dstore: DaemonStore, key: str = "1") -> str:
    """A queued item's id as the store keeps it — the spelling a mark is
    written under, and the one the triggers match."""
    dstore.upsert_new(gh_item(key), 1.0)
    stored = dstore.get(f"gh:{key}")
    assert stored is not None
    return stored.item_id


def _failed(dstore: DaemonStore, key: str = "1", run: str = "r1") -> str:
    """An item that ran and failed for good; its id as stored."""
    item_id = _admitted(dstore, key)
    dstore.mark_running(item_id, run, 2.0)
    dstore.mark_failed(item_id, "boom", 3.0, requeue=False)
    stored = dstore.get(item_id)
    assert stored is not None and stored.state == "failed"
    return item_id


def _dismiss(dstore: DaemonStore, kind: str, key: str, mark: str = "dismissed") -> bool:
    return dstore.set_work_mark(kind, key, mark, cause=mark, at=10.0, actor=ACTOR, reason="seen")


class TestTheStore:
    def test_a_mark_is_written_once_and_read_back(self, dstore: DaemonStore) -> None:
        key = _failed(dstore)
        assert _dismiss(dstore, "item", key)
        mark = dstore.work_mark("item", key, "dismissed")
        assert mark is not None and mark.actor == ACTOR and mark.reason == "seen"
        assert mark.cause == "dismissed" and mark.at == 10.0
        # Who dismissed first stays who dismissed.
        assert not dstore.set_work_mark("item", key, "dismissed", cause="abandoned", at=99.0)
        assert dstore.work_mark("item", key, "dismissed") == mark

    def test_a_page_of_marks_is_one_read_keyed_by_subject(self, dstore: DaemonStore) -> None:
        one, two = _failed(dstore, "1", "r1"), _failed(dstore, "2", "r2")
        _dismiss(dstore, "item", one)
        _dismiss(dstore, "run", "r2")
        found = dstore.work_marks(item_ids=[one, two], run_ids=["r1", "r2"])
        assert set(found) == {("item", one, "dismissed"), ("run", "r2", "dismissed")}
        assert dstore.work_marks() == {}

    def test_clearing_says_whether_a_mark_stood(self, dstore: DaemonStore) -> None:
        key = _failed(dstore)
        _dismiss(dstore, "item", key)
        assert dstore.clear_work_mark("item", key, "dismissed")
        assert not dstore.clear_work_mark("item", key, "dismissed")
        assert dstore.work_mark("item", key, "dismissed") is None

    def test_writing_a_mark_leaves_the_item_s_revision_alone(self, dstore: DaemonStore) -> None:
        """The reason the mark is a side table: a person who read the item
        before someone dismissed it must not have their next command
        refused as stale."""
        key = _failed(dstore)
        before = dstore.get(key)
        _dismiss(dstore, "item", key)
        after = dstore.get(key)
        assert before is not None and after is not None and before.revision == after.revision


class TestAnItemThatMovesLosesItsDismissal:
    def test_a_retry_that_fails_again_is_a_new_alert(self, dstore: DaemonStore) -> None:
        key = _failed(dstore)
        _dismiss(dstore, "item", key)
        dstore.retry(key, 4.0)
        assert dstore.work_mark("item", key, "dismissed") is None
        dstore.mark_running(key, "r2", 5.0)
        dstore.mark_failed(key, "boom again", 6.0, requeue=False)
        assert dstore.work_mark("item", key, "dismissed") is None

    def test_blocked_then_queued_then_blocked_again_is_a_new_alert(
        self, dstore: DaemonStore
    ) -> None:
        """Back in the state it was dismissed in, with no run in between: a
        comparison of fingerprints would call that unchanged."""
        key = _admitted(dstore)
        dstore.mark_blocked(key, "needs a decision", 2.0)
        _dismiss(dstore, "item", key)
        dstore.retry(key, 3.0)
        dstore.mark_blocked(key, "needs a decision", 4.0)
        assert dstore.work_mark("item", key, "dismissed") is None

    def test_a_write_that_keeps_the_state_keeps_the_dismissal(self, dstore: DaemonStore) -> None:
        """Delivering the report an abandon owes rewrites the row — and
        bumps its revision — without moving it anywhere."""
        key = _admitted(dstore)
        dstore.abandon(key, "gave up", 2.0)
        _dismiss(dstore, "item", key)
        revision = dstore.get(key).revision  # type: ignore[union-attr]
        assert dstore.take_pending_report(key)
        assert dstore.get(key).revision > revision  # type: ignore[union-attr]
        assert dstore.work_mark("item", key, "dismissed") is not None

    def test_a_discarded_row_takes_its_marks_with_it(self, dstore: DaemonStore) -> None:
        """A later item may be admitted under the same id."""
        key = _admitted(dstore)
        _dismiss(dstore, "item", key)
        _dismiss(dstore, "item", key, "deleted")
        assert dstore.discard(key)
        assert dstore.work_marks(item_ids=[key]) == {}

    def test_another_item_s_marks_are_untouched(self, dstore: DaemonStore) -> None:
        one, two = _failed(dstore, "1", "r1"), _failed(dstore, "2", "r2")
        _dismiss(dstore, "item", one)
        _dismiss(dstore, "item", two)
        dstore.retry(one, 4.0)
        assert dstore.work_mark("item", two, "dismissed") is not None


class TestADeletedMark:
    def test_it_survives_a_move_between_resting_states(self, dstore: DaemonStore) -> None:
        key = _admitted(dstore)
        dstore.mark_blocked(key, "needs a decision", 2.0)
        _dismiss(dstore, "item", key, "deleted")
        dstore.mark_cancelled(key, "stopped", 3.0)
        assert dstore.work_mark("item", key, "deleted") is not None

    def test_re_admitted_work_is_visible_again(self, dstore: DaemonStore) -> None:
        key = _failed(dstore)
        _dismiss(dstore, "item", key, "deleted")
        dstore.retry(key, 4.0)
        assert dstore.work_mark("item", key, "deleted") is None

    def test_the_trigger_names_the_states_the_store_calls_terminal(self) -> None:
        assert set(RESTING_ITEM_STATES) == TERMINAL_ITEM_STATES


class TestARun:
    def test_a_run_that_moves_loses_its_own_dismissal(self, tmp_path: Path) -> None:
        path = LanternHome(tmp_path).state_db
        dstore, store = DaemonStore(path), StateStore(path)
        try:
            store.create_run("r1", "an outcome")
            store.set_run_state("r1", "failed")
            _dismiss(dstore, "run", "r1")
            _dismiss(dstore, "run", "r1", "deleted")
            store.set_run_state("r1", "building")
            assert dstore.work_mark("run", "r1", "dismissed") is None
            # Deleted work stays hidden: only an item coming back un-hides it.
            assert dstore.work_mark("run", "r1", "deleted") is not None
        finally:
            store.close()
            dstore.close()

    def test_a_run_settling_under_its_item_leaves_the_item_s_mark(self, tmp_path: Path) -> None:
        """An operator's abandon settles the item first and cancels the run
        after; the run's move must not bring the alert back."""
        path = LanternHome(tmp_path).state_db
        dstore, store = DaemonStore(path), StateStore(path)
        try:
            store.create_run("r1", "an outcome")
            key = _admitted(dstore)
            dstore.mark_running(key, "r1", 2.0)
            dstore.abandon(key, "gave up", 3.0)
            _dismiss(dstore, "item", key)
            store.set_run_state("r1", "cancelled")
            assert dstore.work_mark("item", key, "dismissed") is not None
        finally:
            store.close()
            dstore.close()


class TestTheMigration:
    def test_the_frozen_ddl_is_the_models(self) -> None:
        """A database upgraded in place and one built from the metadata
        carry the same triggers, statement for statement."""
        spec = importlib.util.spec_from_file_location(
            "revision_0049", MIGRATIONS_DIR / "versions" / "0049_work_marks.py"
        )
        assert spec is not None and spec.loader is not None
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        assert revision._TRIGGERS == TRIGGERS

    def test_a_deployed_database_gains_the_table_and_its_triggers(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _upgrade_to(path, "0048")
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO daemon_work_items (item_id, source_key, title, state, "
                "created_at, updated_at) VALUES ('gh:issue:1', '1', 'Do 1', 'failed', 1, 1)"
            )
        _head(path)
        triggers = {name for (name,) in _query(path, "SELECT name FROM sqlite_master")}
        assert set(TRIGGERS) <= triggers and "daemon_work_marks" in triggers
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO daemon_work_marks (subject_kind, subject_key, mark, cause, at) "
                "VALUES ('item', 'gh:issue:1', 'dismissed', 'dismissed', 2)"
            )
            db.execute("UPDATE daemon_work_items SET state = 'queued' WHERE item_id = 'gh:issue:1'")
        assert _query(path, "SELECT COUNT(*) FROM daemon_work_marks") == [(0,)]

    def test_it_runs_again_on_a_rewound_stamp(self, tmp_path: Path) -> None:
        path = tmp_path / "state.db"
        _head(path)
        with sqlite3.connect(path) as db:
            db.execute(
                "INSERT INTO daemon_work_marks (subject_kind, subject_key, mark, cause, at) "
                "VALUES ('run', 'r1', 'dismissed', 'dismissed', 2)"
            )
        _stamp(path, "0048")
        _head(path)
        assert _query(path, "SELECT subject_key FROM daemon_work_marks") == [("r1",)]
