"""Plans follow the forge after publish: what reconciliation last read.

``daemon_plans`` gains when the forge was last read into the plan and what
stopped the last attempt. ``daemon_plan_nodes`` gains the issue's
``updated_at`` as last read, why a node no longer follows its issue
(detached), whether its marker is gone, why its managed children checklist
could not be read, and the forge's changes nobody has marked seen (a JSON
array). Columns only, each nullable or defaulted; re-runnable.

Revision ID: 0043
Revises: 0042
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None

_Columns = tuple[tuple[str, Any, str | None], ...]

_PLANS: _Columns = (
    ("reconciled_at", sa.REAL(), None),
    ("reconcile_error", sa.Text(), None),
)
_NODES: _Columns = (
    ("forge_updated_at", sa.Text(), None),
    ("forge_detached", sa.Text(), None),
    ("forge_marker_missing", sa.Integer(), "0"),
    ("forge_checklist_error", sa.Text(), None),
    ("drift_json", sa.Text(), "'[]'"),
)


def _add(
    table: str, columns: tuple[tuple[str, sa.types.TypeEngine[object], str | None], ...]
) -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(table):
        return
    present = {column["name"] for column in inspector.get_columns(table)}
    for name, kind, default in columns:
        if name in present:
            continue
        if default is None:
            op.add_column(table, sa.Column(name, kind, nullable=True))
        else:
            op.add_column(
                table, sa.Column(name, kind, nullable=False, server_default=sa.text(default))
            )


def upgrade() -> None:
    _add("daemon_plans", _PLANS)
    _add("daemon_plan_nodes", _NODES)


def downgrade() -> None:
    with op.batch_alter_table("daemon_plan_nodes") as batch:
        for name, _, _ in reversed(_NODES):
            batch.drop_column(name)
    with op.batch_alter_table("daemon_plans") as batch:
        for name, _, _ in reversed(_PLANS):
            batch.drop_column(name)
