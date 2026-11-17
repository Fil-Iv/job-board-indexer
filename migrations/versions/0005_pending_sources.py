"""pending_sources: career pages submitted through /add or POST /sources for review

The website's read-only database role (``jobindex_web``, docs/DEPLOY.md) gets INSERT and
SELECT on this one table only (owner approval 2026-11-17), so the public demo can queue
requests without any other write access. The grant is skipped where the role does not
exist (local docker, tests).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

WEB_ROLE = "jobindex_web"


def upgrade() -> None:
    op.create_table(
        "pending_sources",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("platform", sa.String(64), nullable=True),
        sa.Column("board_identifier", sa.String(512), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("robots_allowed", sa.Boolean(), nullable=True),
        sa.Column("preview_jobs", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Integer(), nullable=True, server_default=sa.text("NULL")),
        sa.CheckConstraint(
            "status IN ('pending', 'checked', 'added', 'rejected')",
            name="ck_pending_sources_status",
        ),
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{WEB_ROLE}') THEN
            GRANT SELECT, INSERT ON pending_sources TO {WEB_ROLE};
            GRANT USAGE ON SEQUENCE pending_sources_id_seq TO {WEB_ROLE};
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.drop_table("pending_sources")
