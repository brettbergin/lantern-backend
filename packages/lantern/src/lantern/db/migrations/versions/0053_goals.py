"""Goals: the standing objectives an owner writes for a repository.

``daemon_goals`` is one row per goal: the repository it is for, a title,
the objective in the owner's words, whether it is ``active``, ``paused``
or ``done``, who wrote it, and a revision every edit bumps. A plan names
the goal it was proposed from in ``daemon_plans.goal_id`` (revision
0051). One new table, nothing altered, and no row is written: an upgraded
installation has no goals until an owner writes one. Re-runnable on a
rewound stamp.

Revision ID: 0053
Revises: 0052
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "daemon_goals" not in tables:
        op.create_table(
            "daemon_goals",
            sa.Column("goal_id", sa.Text(), primary_key=True),
            sa.Column("repository", sa.Text(), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("state", sa.Text(), nullable=False, server_default=sa.text("'active'")),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_by_display", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
        op.create_index("idx_daemon_goals_repository", "daemon_goals", ["repository", "state"])
    plan_indexes = {
        index["name"] for index in sa.inspect(op.get_bind()).get_indexes("daemon_plans")
    }
    if "idx_daemon_plans_goal" not in plan_indexes:
        op.create_index("idx_daemon_plans_goal", "daemon_plans", ["goal_id"])


def downgrade() -> None:
    op.drop_index("idx_daemon_plans_goal", table_name="daemon_plans")
    op.drop_index("idx_daemon_goals_repository", table_name="daemon_goals")
    op.drop_table("daemon_goals")
