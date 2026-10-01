"""Separate planning input from unpublished, person-authored roots.

Published issues and archived plans retain their reviewed content. Existing
drafts keep their entire tree and brief, but must generate a root before
publishing. Re-runnable when the migration stamp is rewound.

Revision ID: 0048
Revises: 0047
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "input_json" not in {c["name"] for c in sa.inspect(bind).get_columns("daemon_plans")}:
        op.add_column(
            "daemon_plans",
            sa.Column("input_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        )
    rows = (
        bind.execute(
            sa.text(
                "SELECT n.* FROM daemon_plan_nodes n JOIN daemon_plans p "
                "ON p.root_node_id = n.node_id WHERE p.state != 'archived' "
                "AND p.input_json = '{}' AND n.origin = 'person' AND n.state != 'published'"
            )
        )
        .mappings()
        .all()
    )
    for row in rows:
        brief = {key: row[key] for key in ("title", "goal", "context", "non_goals", "constraints")}
        brief["acceptance_criteria"] = json.loads(row["acceptance_criteria_json"])
        bind.execute(
            sa.text(
                "UPDATE daemon_plans SET input_json = :brief, revision = revision + 1 "
                "WHERE plan_id = :id"
            ),
            {"brief": json.dumps(brief), "id": row["plan_id"]},
        )
        bind.execute(
            sa.text(
                "UPDATE daemon_plan_nodes SET title = :title, goal = '', context = '', "
                "non_goals = '', constraints = '', acceptance_criteria_json = '[]', "
                "state = 'draft' "
                "WHERE node_id = :id"
            ),
            {"title": f"Unplanned {row['level']}", "id": row["node_id"]},
        )


def downgrade() -> None:
    op.drop_column("daemon_plans", "input_json")
