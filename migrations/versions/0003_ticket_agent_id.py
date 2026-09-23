"""Rename evaluation_tickets.evaluation_profile_id to agent_id.

The column stores the payload agent UUID that selected the metrics.
Fresh databases created from the current models already use agent_id.

Revision ID: 0003_ticket_agent_id
Revises: 0002_job_idempotency
Create Date: 2026-09-23
"""

from alembic import op
from sqlalchemy import inspect

revision = "0003_ticket_agent_id"
down_revision = "0002_job_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("evaluation_tickets")}
    if "evaluation_profile_id" in columns and "agent_id" not in columns:
        op.alter_column("evaluation_tickets", "evaluation_profile_id", new_column_name="agent_id")
    indexes = {index["name"] for index in inspect(bind).get_indexes("evaluation_tickets")}
    if "ix_tickets_profile" in indexes and "ix_tickets_agent" not in indexes:
        op.execute("ALTER INDEX ix_tickets_profile RENAME TO ix_tickets_agent")


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("evaluation_tickets")}
    if "agent_id" in columns and "evaluation_profile_id" not in columns:
        op.alter_column("evaluation_tickets", "agent_id", new_column_name="evaluation_profile_id")
    indexes = {index["name"] for index in inspect(bind).get_indexes("evaluation_tickets")}
    if "ix_tickets_agent" in indexes and "ix_tickets_profile" not in indexes:
        op.execute("ALTER INDEX ix_tickets_agent RENAME TO ix_tickets_profile")
