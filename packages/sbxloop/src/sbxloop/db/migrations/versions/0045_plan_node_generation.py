"""Keep a plan node's clarifying questions and a person's answers.

A ``plan`` run may ask a person questions before it proposes a node's next
level (#2345). The questions, the run that asked them, and the answers (or
the skip) live on the node: ``daemon_plan_nodes.generation_json``, NULL
until a breakdown of the node asks something.

Additive: one nullable column nothing older reads. Re-runnable: the column
is added only when it is not already there.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_TABLE = "daemon_plan_nodes"
_COLUMN = "generation_json"


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
