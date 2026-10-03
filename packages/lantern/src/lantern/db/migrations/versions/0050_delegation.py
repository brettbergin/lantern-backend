"""Delegation: the grants an owner writes and the ledger of what was decided.

``daemon_grants`` is one row per standing rule (an agent, an action it may
take, the conditions, a daily limit). ``daemon_decisions`` is one row per
judged act: the outcome, the reason, the grant that allowed it, what it was
about and the facts it was judged on, and for an escalation how it was
resolved. Two new tables, nothing altered, and no row is written: an
upgraded installation delegates nothing until an owner adds a grant.
Re-runnable on a rewound stamp.

Revision ID: 0050
Revises: 0049
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "daemon_grants" not in tables:
        op.create_table(
            "daemon_grants",
            sa.Column("grant_id", sa.Text(), primary_key=True),
            sa.Column("agent_slug", sa.Text(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("conditions_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("daily_limit", sa.Integer(), nullable=True),
            sa.Column("enabled", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_by", sa.Text(), nullable=True),
            sa.Column("created_by_display", sa.Text(), nullable=True),
            sa.Column("created_at", sa.REAL(), nullable=False),
            sa.Column("updated_at", sa.REAL(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        )
        op.create_index("idx_daemon_grants_subject", "daemon_grants", ["agent_slug", "action"])
    if "daemon_decisions" not in tables:
        op.create_table(
            "daemon_decisions",
            sa.Column("decision_id", sa.Text(), primary_key=True),
            sa.Column("grant_id", sa.Text(), nullable=True),
            sa.Column("agent_slug", sa.Text(), nullable=False),
            sa.Column("action", sa.Text(), nullable=False),
            sa.Column("outcome", sa.Text(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("plan_id", sa.Text(), nullable=True),
            sa.Column("node_id", sa.Text(), nullable=True),
            sa.Column("item_id", sa.Text(), nullable=True),
            sa.Column("run_id", sa.Text(), nullable=True),
            sa.Column("epic_run_id", sa.Text(), nullable=True),
            sa.Column("repository", sa.Text(), nullable=True),
            sa.Column("operation_id", sa.Text(), nullable=True),
            sa.Column("attrs_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("at", sa.REAL(), nullable=False),
            sa.Column("resolved_at", sa.REAL(), nullable=True),
            sa.Column("resolved_by", sa.Text(), nullable=True),
            sa.Column("resolution", sa.Text(), nullable=True),
        )
        op.create_index("idx_daemon_decisions_at", "daemon_decisions", ["at", "decision_id"])
        op.create_index("idx_daemon_decisions_grant", "daemon_decisions", ["grant_id", "at"])


def downgrade() -> None:
    op.drop_index("idx_daemon_decisions_grant", table_name="daemon_decisions")
    op.drop_index("idx_daemon_decisions_at", table_name="daemon_decisions")
    op.drop_table("daemon_decisions")
    op.drop_index("idx_daemon_grants_subject", table_name="daemon_grants")
    op.drop_table("daemon_grants")
