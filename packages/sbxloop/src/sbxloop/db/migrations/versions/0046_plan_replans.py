"""A re-plan's diff waits on the node it re-plans.

``daemon_plan_nodes`` gains ``replan_json``: the diff a ``plan`` run
proposed against a published node's children (entries to add, modify or
close, with the run and when), kept until a person approves or discards
each entry. NULL when no diff is waiting.

Additive: one nullable column nothing older reads. Re-runnable: it checks
what is already there.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_TABLE = "daemon_plan_nodes"
_COLUMN = "replan_json"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(_TABLE):
        return
    if _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}:
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_COLUMN)
