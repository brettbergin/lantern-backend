"""Plans: who proposed, approved and published a node, a level's review,
and whether a plan may advance on its own.

``daemon_plan_nodes`` gains ``proposed_by``, ``approved_by`` and
``published_by`` (the id of the person or ``agent:<slug>`` that did each;
NULL where nobody is recorded) and ``review_json`` (a reviewer's verdict on
the node's level; NULL until one is given). ``daemon_plans`` gains
``advance`` (``manual`` or ``auto``) and ``goal_id`` (the goal the plan was
proposed from; NULL for a plan a person drafted).

Additive: every column is nullable or defaulted, so a build before this
revision still reads and writes the tables. No row is rewritten — every
existing plan reads ``manual`` and every existing node reads with nobody
recorded. Re-runnable on a rewound stamp.

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

_NODE_COLUMNS = ("proposed_by", "approved_by", "published_by", "review_json")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    nodes = {c["name"] for c in inspector.get_columns("daemon_plan_nodes")}
    for name in _NODE_COLUMNS:
        if name not in nodes:
            op.add_column("daemon_plan_nodes", sa.Column(name, sa.Text(), nullable=True))
    plans = {c["name"] for c in inspector.get_columns("daemon_plans")}
    if "advance" not in plans:
        op.add_column(
            "daemon_plans",
            sa.Column("advance", sa.Text(), nullable=False, server_default=sa.text("'manual'")),
        )
    if "goal_id" not in plans:
        op.add_column("daemon_plans", sa.Column("goal_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("daemon_plans", "goal_id")
    op.drop_column("daemon_plans", "advance")
    for name in reversed(_NODE_COLUMNS):
        op.drop_column("daemon_plan_nodes", name)
