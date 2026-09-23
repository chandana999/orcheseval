"""Initial schema: jobs, tickets, and results.

Checks live in the dataset folder's config.json. This revision does not create
profile, metric, dataset, payload, or config tables.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-22
"""

from alembic import op

from evalorch.db.models import (
    CHECK_TYPE,
    JOB_STATUS,
    RESULT_STATUS,
    TICKET_STATUS,
    Base,
)

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (CHECK_TYPE, JOB_STATUS, TICKET_STATUS, RESULT_STATUS):
        enum.create(bind, checkfirst=True)
    Base.metadata.create_all(bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind)
    for enum in (RESULT_STATUS, TICKET_STATUS, JOB_STATUS, CHECK_TYPE):
        enum.drop(bind, checkfirst=True)
