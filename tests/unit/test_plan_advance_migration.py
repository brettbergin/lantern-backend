"""Revision 0051: who proposed, approved and published a node, a level's
review, and a plan's ``advance`` switch and goal.

A database written before the revision holds plans with none of them. After
the upgrade every plan reads ``advance = "manual"`` with no goal, every node
reads with nobody recorded and no review, and running the upgrade again on
a rewound stamp changes nothing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from lantern.daemon.store import DaemonStore
from lantern.plans.store import PlanStore
from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to

NODE_COLUMNS = ("proposed_by", "approved_by", "published_by", "review_json")
PLAN_COLUMNS = ("advance", "goal_id")


def _columns(path: Path, table: str) -> list[str]:
    return [str(row[1]) for row in _query(path, f"PRAGMA table_info({table})")]


def _seed(path: Path) -> None:
    """What a release before the revision wrote for one plan."""
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO daemon_plans (plan_id, workspace_id, root_node_id, state, "
            "created_at, updated_at, revision) VALUES ('plan_1', 'default', 'root', 'active', "
            "1, 1, 3)"
        )
        db.execute(
            "INSERT INTO daemon_plan_nodes (node_id, plan_id, position, level, repository, "
            "state, origin, title, created_at, updated_at) VALUES "
            "('root', 'plan_1', 0, 'epic', 'o/r', 'published', 'planner', 'An epic', 1, 1)"
        )
        db.execute(
            "INSERT INTO daemon_plan_nodes (node_id, plan_id, parent_id, position, level, "
            "repository, state, origin, title, created_at, updated_at) VALUES "
            "('task', 'plan_1', 'root', 0, 'task', 'o/r', 'approved', 'person', 'A task', 1, 1)"
        )


def test_existing_plans_read_manual_with_nobody_recorded(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0050")
    assert not set(NODE_COLUMNS) & set(_columns(path, "daemon_plan_nodes"))
    assert not set(PLAN_COLUMNS) & set(_columns(path, "daemon_plans"))
    _seed(path)

    _upgrade_to(path, "0051")

    # Appended, in this order: the models declare them the same way.
    assert tuple(_columns(path, "daemon_plan_nodes")[-4:]) == NODE_COLUMNS
    assert tuple(_columns(path, "daemon_plans")[-2:]) == PLAN_COLUMNS
    assert _query(path, "SELECT advance, goal_id, revision FROM daemon_plans") == [
        ("manual", None, 3)
    ]
    assert _query(
        path,
        "SELECT proposed_by, approved_by, published_by, review_json FROM daemon_plan_nodes "
        "ORDER BY node_id",
    ) == [(None, None, None, None), (None, None, None, None)]


def test_the_upgrade_runs_again_on_a_rewound_stamp(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0050")
    _seed(path)
    _upgrade_to(path, "0051")
    with sqlite3.connect(path) as db:
        db.execute("UPDATE daemon_plans SET advance = 'auto', goal_id = 'goal_1'")
        db.execute("UPDATE daemon_plan_nodes SET approved_by = 'usr_1' WHERE node_id = 'task'")

    _stamp(path, "0050")
    _upgrade_to(path, "0051")

    assert _query(path, "SELECT advance, goal_id FROM daemon_plans") == [("auto", "goal_1")]
    assert _query(path, "SELECT approved_by FROM daemon_plan_nodes WHERE node_id = 'task'") == [
        ("usr_1",)
    ]


def test_a_build_before_the_revision_still_writes_a_plan(tmp_path: Path) -> None:
    """The additive contract: a rolled-back daemon inserts a plan row naming
    none of the new columns, and the row it writes reads ``manual``."""
    path = tmp_path / "state.db"
    _head(path)
    _seed(path)
    assert _query(path, "SELECT advance, goal_id FROM daemon_plans") == [("manual", None)]


def test_the_store_reads_a_plan_written_before_the_revision(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0050")
    _seed(path)

    plan = PlanStore(DaemonStore(path)).get("plan_1")

    assert plan is not None
    assert (plan.advance, plan.goal_id) == ("manual", None)
    for node in plan.nodes:
        assert (node.proposed_by, node.approved_by, node.published_by) == (None, None, None)
        assert node.review is None
