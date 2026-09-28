"""Existing draft briefs move out of issue nodes, once, without losing the tree."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.unit.test_channel_membership_migration import _head, _query, _stamp, _upgrade_to


def test_existing_drafts_are_repaired_once_and_published_content_stays(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    _upgrade_to(path, "0047")
    with sqlite3.connect(path) as db:
        for name, state, origin in (
            ("draft", "draft", "person"),
            ("published", "published", "person"),
            ("generated", "proposed", "planner"),
        ):
            db.execute(
                "INSERT INTO daemon_plans (plan_id, workspace_id, root_node_id, state, "
                "created_at, updated_at, revision) VALUES (?, 'default', ?, 'active', 1, 1, 3)",
                (name, name),
            )
            db.execute(
                "INSERT INTO daemon_plan_nodes (node_id, plan_id, position, level, repository, "
                "state, origin, title, goal, acceptance_criteria_json, created_at, updated_at) "
                "VALUES (?, ?, 0, 'epic', 'o/r', ?, ?, 'raw ask', 'user goal', "
                "'[\"criterion\"]', 1, 1)",
                (name, name, state, origin),
            )
        db.execute(
            "INSERT INTO daemon_plan_nodes (node_id, plan_id, parent_id, position, level, "
            "repository, state, origin, title, created_at, updated_at) VALUES "
            "('child', 'draft', 'draft', 0, 'task', 'o/r', 'proposed', 'planner', "
            "'Generated task', 1, 1)"
        )
    _head(path)
    brief, revision = _query(
        path, "SELECT input_json, revision FROM daemon_plans WHERE plan_id = 'draft'"
    )[0]
    assert json.loads(str(brief))["goal"] == "user goal" and revision == 4
    assert _query(
        path,
        "SELECT title, goal, acceptance_criteria_json FROM daemon_plan_nodes "
        "WHERE node_id = 'draft'",
    ) == [("Unplanned epic", "", "[]")]
    assert _query(path, "SELECT title FROM daemon_plan_nodes WHERE node_id = 'child'") == [
        ("Generated task",)
    ]
    assert _query(
        path, "SELECT title FROM daemon_plan_nodes WHERE node_id IN ('published', 'generated')"
    ) == [("raw ask",), ("raw ask",)]
    _stamp(path, "0047")
    _head(path)
    assert _query(
        path, "SELECT input_json, revision FROM daemon_plans WHERE plan_id = 'draft'"
    ) == [(brief, 4)]
