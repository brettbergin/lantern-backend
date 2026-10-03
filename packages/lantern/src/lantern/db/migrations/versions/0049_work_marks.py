"""Marks on work: an alert a person dismissed, work a person deleted.

``daemon_work_marks`` is one row per mark on an item or a run, and three
triggers drop a mark when the work it stands on moves again, so a dismissed
alert comes back when there is something new to look at. A new table and
triggers that touch nothing an older release reads. Re-runnable on a
rewound stamp.

The trigger DDL is frozen here, as every revision's is: what a deployed
database was upgraded with must not change when the models do.

Revision ID: 0049
Revises: 0048
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None

_TRIGGERS: dict[str, str] = {
    "work_marks_item_state": (
        "CREATE TRIGGER IF NOT EXISTS work_marks_item_state "
        "AFTER UPDATE OF state ON daemon_work_items "
        "WHEN NEW.state IS NOT OLD.state "
        "BEGIN DELETE FROM daemon_work_marks "
        "WHERE subject_kind = 'item' AND subject_key = NEW.item_id "
        "AND (mark = 'dismissed' OR NEW.state NOT IN "
        "('blocked', 'cancelled', 'done', 'failed')); END"
    ),
    "work_marks_item_delete": (
        "CREATE TRIGGER IF NOT EXISTS work_marks_item_delete "
        "AFTER DELETE ON daemon_work_items "
        "BEGIN DELETE FROM daemon_work_marks "
        "WHERE subject_kind = 'item' AND subject_key = OLD.item_id; END"
    ),
    "work_marks_run_state": (
        "CREATE TRIGGER IF NOT EXISTS work_marks_run_state "
        "AFTER UPDATE OF state ON runs "
        "WHEN NEW.state IS NOT OLD.state "
        "BEGIN DELETE FROM daemon_work_marks "
        "WHERE subject_kind = 'run' AND subject_key = NEW.run_id "
        "AND mark = 'dismissed'; END"
    ),
}


def upgrade() -> None:
    if "daemon_work_marks" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "daemon_work_marks",
            sa.Column("subject_kind", sa.Text(), nullable=False),
            sa.Column("subject_key", sa.Text(), nullable=False),
            sa.Column("mark", sa.Text(), nullable=False),
            sa.Column("cause", sa.Text(), nullable=False),
            sa.Column("at", sa.REAL(), nullable=False),
            sa.Column("actor_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("operation_id", sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint("subject_kind", "subject_key", "mark"),
        )
    for ddl in _TRIGGERS.values():
        op.execute(sa.text(ddl))


def downgrade() -> None:
    for name in _TRIGGERS:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {name}"))
    op.drop_table("daemon_work_marks")
