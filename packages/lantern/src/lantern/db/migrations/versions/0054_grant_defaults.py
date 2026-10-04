"""Grants: who wrote each one, and which of Lantern's defaults it is.

``daemon_grants`` gains ``source`` (``default`` for a grant Lantern seeded,
``owner`` for one a person wrote; every row already stored is an owner's)
and ``default_key`` (the stable name of the default a seeded grant is, such
as ``plan.approve:critic:v1``; NULL on an owner's grant). A unique index on
``default_key`` keeps two processes starting at once from seeding the same
default twice.

No row is written here: the defaults are seeded by the daemon when it
starts, which records each key it seeded in ``daemon_state`` so a default
an owner deleted is never seeded again. Re-runnable on a rewound stamp.

Revision ID: 0054
Revises: 0053
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

_TABLE = "daemon_grants"
_INDEX = "idx_daemon_grants_default_key"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    if "source" not in present:
        op.add_column(
            _TABLE,
            sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'owner'")),
        )
    if "default_key" not in present:
        op.add_column(_TABLE, sa.Column("default_key", sa.Text(), nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, ["default_key"], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "default_key")
    op.drop_column(_TABLE, "source")
