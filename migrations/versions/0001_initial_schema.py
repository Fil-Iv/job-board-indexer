"""initial schema: sources, jobs, fetch_runs, job_changes

Revision ID: 0001
Revises:
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_VECTOR_SQL = (
    "setweight(to_tsvector('english'::regconfig, coalesce(title, '')), 'A') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(company, '')), 'B') || "
    "setweight(to_tsvector('english'::regconfig, coalesce(description_text, '')), 'C')"
)


def upgrade() -> None:
    op.create_table(
        "sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("board_identifier", sa.String(length=512), nullable=False),
        sa.Column("company_name", sa.String(length=512), nullable=True),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("platform", "board_identifier", name=op.f("uq_sources_platform_board_identifier")),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=512), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("company", sa.Text(), nullable=True),
        sa.Column("description_html", sa.Text(), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=True),
        sa.Column("location_raw", sa.Text(), nullable=True),
        sa.Column("city", sa.String(length=255), nullable=True),
        sa.Column("region", sa.String(length=255), nullable=True),
        sa.Column("country", sa.String(length=255), nullable=True),
        sa.Column("remote_type", sa.String(length=16), nullable=False),
        sa.Column("employment_type", sa.String(length=64), nullable=True),
        sa.Column("salary_min", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("salary_max", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("salary_currency", sa.String(length=3), nullable=True),
        sa.Column("salary_period", sa.String(length=16), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canonical_job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_VECTOR_SQL, persisted=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_jobs_source_id_sources"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["canonical_job_id"], ["jobs.id"], name=op.f("fk_jobs_canonical_job_id_jobs"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.CheckConstraint("remote_type IN ('onsite', 'hybrid', 'remote', 'unknown')", name=op.f("ck_jobs_remote_type")),
        sa.CheckConstraint("status IN ('open', 'closed')", name=op.f("ck_jobs_job_status")),
        sa.UniqueConstraint("source_id", "external_id", name=op.f("uq_jobs_source_id_external_id")),
    )
    op.create_index(op.f("ix_jobs_source_id"), "jobs", ["source_id"])
    op.create_index(op.f("ix_jobs_content_hash"), "jobs", ["content_hash"])
    op.create_index(op.f("ix_jobs_canonical_job_id"), "jobs", ["canonical_job_id"])
    op.create_index("ix_jobs_status_posted_at", "jobs", ["status", "posted_at"])
    op.create_index("ix_jobs_search_vector", "jobs", ["search_vector"], postgresql_using="gin")

    op.create_table(
        "fetch_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("jobs_found", sa.Integer(), nullable=False),
        sa.Column("jobs_new", sa.Integer(), nullable=False),
        sa.Column("jobs_updated", sa.Integer(), nullable=False),
        sa.Column("jobs_closed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["source_id"], ["sources.id"], name=op.f("fk_fetch_runs_source_id_sources"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_runs")),
        sa.CheckConstraint("status IN ('running', 'success', 'failed')", name=op.f("ck_fetch_runs_fetch_status")),
    )
    op.create_index("ix_fetch_runs_source_id_started_at", "fetch_runs", ["source_id", "started_at"])

    op.create_table(
        "job_changes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=False),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("field", sa.String(length=64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_job_changes_job_id_jobs"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_changes")),
    )
    op.create_index("ix_job_changes_job_id_changed_at", "job_changes", ["job_id", "changed_at"])


def downgrade() -> None:
    op.drop_table("job_changes")
    op.drop_table("fetch_runs")
    op.drop_table("jobs")
    op.drop_table("sources")
