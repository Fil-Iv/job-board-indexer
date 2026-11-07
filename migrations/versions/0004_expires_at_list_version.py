"""jobs.expires_at and jobs.list_version (generic sitemap + JSON-LD connectors)

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("jobs", sa.Column("list_version", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "list_version")
    op.drop_column("jobs", "expires_at")
