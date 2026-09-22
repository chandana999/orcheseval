"""Add a unique idempotency key on evaluation jobs.

Revision ID: 0002_job_idempotency
Revises: 0001_initial
Create Date: 2026-09-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_job_idempotency"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in inspect(bind).get_columns("evaluation_jobs")}
    if "idempotency_key" not in columns:
        op.add_column("evaluation_jobs", sa.Column("idempotency_key", sa.Text(), nullable=True))
    if "idempotency_request_hash" not in columns:
        op.add_column(
            "evaluation_jobs",
            sa.Column("idempotency_request_hash", sa.Text(), nullable=True),
        )
    indexes = {index["name"] for index in inspect(bind).get_indexes("evaluation_jobs")}
    if "uq_evaluation_jobs_idempotency_key" not in indexes:
        op.create_index(
            "uq_evaluation_jobs_idempotency_key",
            "evaluation_jobs",
            ["idempotency_key"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index("uq_evaluation_jobs_idempotency_key", table_name="evaluation_jobs")
    op.drop_column("evaluation_jobs", "idempotency_request_hash")
    op.drop_column("evaluation_jobs", "idempotency_key")
