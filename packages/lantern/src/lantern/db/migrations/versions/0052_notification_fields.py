"""Push notifications: the attention entry, the actions and the urgency.

``api_push_notifications`` gains ``entry_id`` (the id of the attention
entry a notification is about; NULL when it is about none),
``actions_json`` (the JSON list of the entry's actions its recipient may
take; NULL on a row written before this revision) and ``level``
(``passive``, ``active`` or ``time_sensitive``; NULL on a row written
before this revision, which reads as ``active``).

Additive: every column is nullable, so a build before this revision still
reads and writes the table. No row is rewritten. Re-runnable on a rewound
stamp.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None

_TABLE = "api_push_notifications"
_COLUMNS = ("entry_id", "actions_json", "level")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    present = {c["name"] for c in inspector.get_columns(_TABLE)}
    for name in _COLUMNS:
        if name not in present:
            op.add_column(_TABLE, sa.Column(name, sa.Text(), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column(_TABLE, name)
