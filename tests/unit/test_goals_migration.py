"""Revision 0053: ``daemon_goals``, the standing objectives an owner writes,
and an index on the plans that name one.

A database written before the revision has neither. After the upgrade the
table is there and empty, the plans already stored are untouched, and
running the upgrade again on a rewound stamp changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lantern.daemon.goals import GoalStore
from lantern.daemon.store import DaemonStore
from tests.unit.test_channel_membership_migration import _query, _stamp, _upgrade_to

COLUMNS = (
    "goal_id",
    "repository",
    "title",
    "text",
    "state",
    "created_by",
    "created_by_display",
    "created_at",
    "updated_at",
    "revision",
)


def _tables(path: Path) -> set[str]:
    return {
        str(row[0]) for row in _query(path, "SELECT name FROM sqlite_master WHERE type='table'")
    }


def _indexes(path: Path, table: str) -> set[str]:
    return {str(row[1]) for row in _query(path, f"PRAGMA index_list({table})")}


def _seed_plan(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO daemon_plans (plan_id, workspace_id, root_node_id, state, "
            "created_at, updated_at, revision, goal_id) VALUES "
            "('plan_1', 'default', 'root', 'active', 1, 1, 3, 'goal_1')"
        )


def test_the_table_arrives_empty_and_plans_are_untouched(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0052")
    assert "daemon_goals" not in _tables(path)
    _seed_plan(path)

    _upgrade_to(path, "0053")

    columns = [str(row[1]) for row in _query(path, "PRAGMA table_info(daemon_goals)")]
    assert tuple(columns) == COLUMNS
    assert _query(path, "SELECT COUNT(*) FROM daemon_goals") == [(0,)]
    assert "idx_daemon_goals_repository" in _indexes(path, "daemon_goals")
    assert "idx_daemon_plans_goal" in _indexes(path, "daemon_plans")
    assert _query(path, "SELECT plan_id, goal_id, revision FROM daemon_plans") == [
        ("plan_1", "goal_1", 3)
    ]


def test_the_upgrade_runs_again_on_a_rewound_stamp(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0053")
    store = GoalStore(DaemonStore(path))
    goal = store.create(
        repository="o/r",
        title="Faster builds",
        text="Cut the build time in half.",
        created_by="usr_1",
        created_by_display="owner",
        now=5.0,
    )

    _stamp(path, "0052")
    _upgrade_to(path, "0053")

    assert GoalStore(DaemonStore(path)).goal(goal.id) == goal
