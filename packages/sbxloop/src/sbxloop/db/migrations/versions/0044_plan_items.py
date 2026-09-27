"""Link a work item to the plan node it proposes the next level of.

A ``plan`` run proposes one level of a plan node — an initiative's epics or
an epic's tasks — and delivers the proposal to the plan record. Its work
item names that node: ``daemon_work_items.plan_id`` and ``plan_node_id``,
with an index on the node so a breakdown can ask whether one is already
queued or running for it.

Additive: two nullable columns nothing older reads, and an index.
Re-runnable: each step checks what is already there.

Revision ID: 0044
Revises: 0043
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None

_TABLE = "daemon_work_items"
_INDEX = "idx_daemon_items_plan_node"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    for column in ("plan_id", "plan_node_id"):
        if column not in columns:
            op.add_column(_TABLE, sa.Column(column, sa.Text(), nullable=True))
    if _INDEX not in {i["name"] for i in inspector.get_indexes(_TABLE)}:
        op.create_index(_INDEX, _TABLE, ["plan_node_id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "plan_node_id")
    op.drop_column(_TABLE, "plan_id")
