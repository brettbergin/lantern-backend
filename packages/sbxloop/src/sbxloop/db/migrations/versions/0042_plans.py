"""Plans: a tree of initiative, epic and task nodes planned into the forge.

``daemon_plans`` is one row per plan (its root node, whether it is archived,
who made it, and the revision every mutation is checked against).
``daemon_plan_nodes`` is one row per node with its named sections and, once
published, the issue it became. Two new tables, nothing altered.
Re-runnable on a rewound stamp.

Revision ID: 0042
Revises: 0041
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "daemon_plans" not in tables:
        op.create_table(
            "daemon_plans",
            sa.Column("plan_id", sa.Text(), primary_key=True),
            sa.Column("workspace_id", sa.Text(), nullable=False),
            sa.Column("root_node_id", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_by_display", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
        op.create_index("idx_daemon_plans_updated", "daemon_plans", ["updated_at"])
    if "daemon_plan_nodes" not in tables:
        op.create_table(
            "daemon_plan_nodes",
            sa.Column("node_id", sa.Text(), primary_key=True),
            sa.Column("plan_id", sa.Text(), nullable=False),
            sa.Column("parent_id", sa.Text(), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("level", sa.Text(), nullable=False),
            sa.Column("repository", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("origin", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("goal", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("context", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column(
                "acceptance_criteria_json",
                sa.Text(),
                nullable=False,
                server_default=sa.text("'[]'"),
            ),
            sa.Column("kind", sa.Text(), nullable=True),
            sa.Column("workload_profile", sa.Text(), nullable=True),
            sa.Column(
                "verify_commands_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")
            ),
            sa.Column("depends_on_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("non_goals", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("constraints", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("forge_number", sa.Integer(), nullable=True),
            sa.Column("forge_url", sa.Text(), nullable=True),
            sa.Column("forge_state", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
        )
        op.create_index(
            "idx_daemon_plan_nodes_plan",
            "daemon_plan_nodes",
            ["plan_id", "parent_id", "position"],
        )


def downgrade() -> None:
    op.drop_index("idx_daemon_plan_nodes_plan", table_name="daemon_plan_nodes")
    op.drop_table("daemon_plan_nodes")
    op.drop_index("idx_daemon_plans_updated", table_name="daemon_plans")
    op.drop_table("daemon_plans")
