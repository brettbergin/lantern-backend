"""Epic runs: an epic's tasks admitted as issue runs in dependency order.

``daemon_plan_epic_runs`` is one row per epic run (the plan and epic it
runs, its state, who started it). ``daemon_plan_epic_run_tasks`` is one row
per task of a run: its state, the item it was admitted as and the run that
item last started. Two new tables, nothing altered. Re-runnable on a
rewound stamp.

Revision ID: 0047
Revises: 0046
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "daemon_plan_epic_runs" not in tables:
        op.create_table(
            "daemon_plan_epic_runs",
            sa.Column("epic_run_id", sa.Text(), primary_key=True),
            sa.Column("plan_id", sa.Text(), nullable=False),
            sa.Column("node_id", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("started_by", sa.Text(), nullable=True),
            sa.Column("started_by_display", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("completed_at", sa.REAL(), nullable=True),
        )
        op.create_index(
            "idx_daemon_plan_epic_runs_node", "daemon_plan_epic_runs", ["plan_id", "node_id"]
        )
    if "daemon_plan_epic_run_tasks" not in tables:
        op.create_table(
            "daemon_plan_epic_run_tasks",
            sa.Column("epic_run_id", sa.Text(), nullable=False),
            sa.Column("node_id", sa.Text(), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("state", sa.Text(), nullable=False),
            sa.Column("item_id", sa.Text(), nullable=True),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("admitted_at", sa.REAL(), nullable=True),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.PrimaryKeyConstraint("epic_run_id", "node_id"),
        )


def downgrade() -> None:
    op.drop_table("daemon_plan_epic_run_tasks")
    op.drop_index("idx_daemon_plan_epic_runs_node", table_name="daemon_plan_epic_runs")
    op.drop_table("daemon_plan_epic_runs")
