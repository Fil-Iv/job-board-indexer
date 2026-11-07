"""parser versioning: jobs.raw_hash/parser_version, job_changes.parser_version/cause

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("raw_hash", sa.String(length=64), nullable=True))
    op.add_column("jobs", sa.Column("parser_version", sa.Integer(), nullable=True))
    # Existing history predates versioning: tag it as parser version 1 / upstream.
    op.add_column(
        "job_changes",
        sa.Column("parser_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "job_changes",
        sa.Column("cause", sa.String(length=16), server_default="upstream", nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_job_changes_change_cause"),
        "job_changes",
        "cause IN ('upstream', 'parser', 'local')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_job_changes_change_cause"), "job_changes", type_="check")
    op.drop_column("job_changes", "cause")
    op.drop_column("job_changes", "parser_version")
    op.drop_column("jobs", "parser_version")
    op.drop_column("jobs", "raw_hash")
