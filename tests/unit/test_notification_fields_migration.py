"""Revision 0052: a stored push notification names the attention entry it
is about, the actions its recipient may take and how urgent it is.

A database written before the revision holds notifications with none of
them. After the upgrade those rows read back with nulls — and the store
serves them as about no entry, with no actions, at the ``active`` level —
and running the upgrade again on a rewound stamp changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lantern.api.push.store import DeviceStore
from lantern.daemon.store import DaemonStore
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to

COLUMNS = ("entry_id", "actions_json", "level")


def _columns(path: Path) -> list[str]:
    return [str(row[1]) for row in _query(path, "PRAGMA table_info(api_push_notifications)")]


def _seed(path: Path) -> None:
    """What a release before the revision wrote for one push."""
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO api_push_notifications (ref, user_id, kind, channel_id, turn_id, "
            "title, body, event_seq, created_at) VALUES ('ntf_old', 'usr_1', 'gate', NULL, "
            "NULL, 'Decision needed', 'Work is waiting for your decision.', 7, 1)"
        )


def test_existing_notifications_read_back_with_nulls(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0051")
    assert not set(COLUMNS) & set(_columns(path))
    _seed(path)

    _upgrade_to(path, "0052")

    # Appended, in this order: the model declares them the same way.
    assert tuple(_columns(path)[-3:]) == COLUMNS
    assert _query(
        path, "SELECT ref, entry_id, actions_json, level FROM api_push_notifications"
    ) == [("ntf_old", None, None, None)]


def test_the_upgrade_runs_again_on_a_rewound_stamp(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0051")
    _seed(path)
    _upgrade_to(path, "0052")
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE api_push_notifications SET entry_id = 'gate:gate_1', "
            "actions_json = '[\"gate_approve\"]', level = 'time_sensitive'"
        )

    _stamp(path, "0051")
    _upgrade_to(path, "0052")

    assert _query(path, "SELECT entry_id, actions_json, level FROM api_push_notifications") == [
        ("gate:gate_1", '["gate_approve"]', "time_sensitive")
    ]


def test_a_build_before_the_revision_still_records_a_notification(tmp_path: Path) -> None:
    """The additive contract: a rolled-back daemon inserts a row naming
    none of the new columns."""
    path = tmp_path / "state.db"
    _head(path)
    _seed(path)
    assert _query(path, "SELECT entry_id, actions_json, level FROM api_push_notifications") == [
        (None, None, None)
    ]


def test_the_store_serves_an_old_row_as_about_nothing_and_active(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0051")
    _seed(path)
    _upgrade_to(path, "0052")

    notification = DeviceStore(DaemonStore(path)).notification("usr_1", "ntf_old")

    assert notification is not None
    assert (notification.entry_id, notification.actions, notification.level) == (
        None,
        (),
        "active",
    )
