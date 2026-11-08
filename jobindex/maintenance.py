"""Database size report and retention (``db-size`` and ``prune`` CLI commands).

The hosted demo runs on the Supabase free tier (500 MB); see
docs/OPERATIONS.md#size-budget for how the limits below keep it under ~400 MB.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.models import FetchRun, FetchStatus, Job, JobChange, JobStatus
from jobindex.models.base import utcnow

MB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class TableSize:
    name: str
    total_bytes: int  # table + indexes + TOAST
    rows: int  # planner estimate (pg_class.reltuples), exact enough for a size report


@dataclass(frozen=True, slots=True)
class DbSize:
    database_bytes: int
    tables: list[TableSize]

    @property
    def database_mb(self) -> float:
        return self.database_bytes / MB


async def db_size(session: AsyncSession) -> DbSize:
    database_bytes = await session.scalar(text("SELECT pg_database_size(current_database())"))
    rows = await session.execute(
        text(
            "SELECT c.relname, pg_total_relation_size(c.oid), greatest(c.reltuples, 0)::bigint"
            " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE c.relkind = 'r' AND n.nspname = current_schema()"
            " ORDER BY 2 DESC"
        )
    )
    return DbSize(
        int(database_bytes or 0),
        [TableSize(str(name), int(size), int(n)) for name, size, n in rows],
    )


def render_db_size(size: DbSize, *, markdown: bool = False) -> str:
    if markdown:
        lines = [
            f"**Database size: {size.database_mb:.1f} MB**",
            "",
            "| Table | Size (MB) | Rows (est.) |",
            "|---|---:|---:|",
        ]
        lines += [f"| {t.name} | {t.total_bytes / MB:.1f} | {t.rows:,} |" for t in size.tables]
    else:
        lines = [f"database: {size.database_mb:.1f} MB"]
        lines += [
            f"  {t.name:<20} {t.total_bytes / MB:8.1f} MB {t.rows:>10,} rows" for t in size.tables
        ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class PruneResult:
    jobs: int = 0
    job_changes: int = 0
    fetch_runs: int = 0


async def prune(
    session: AsyncSession,
    *,
    closed_job_days: int | None,
    job_change_days: int | None,
    fetch_run_days: int | None,
) -> PruneResult:
    """Delete old closed postings (their job_changes cascade), old job_changes and old
    fetch_runs. Open postings are never deleted. ``None`` skips that step.

    Each source's latest successful fetch run is kept whatever its age: the job-drop
    guard (``crawler._check_drop``) compares the next crawl against it. Running runs are
    kept too. Run ``dedup`` afterwards: deleted canonical postings leave
    ``canonical_job_id`` NULL (``ON DELETE SET NULL``) until it regroups them.
    """
    now = utcnow()
    jobs = changes = runs = 0
    if closed_job_days is not None:
        result = await session.execute(
            delete(Job).where(
                Job.status == JobStatus.CLOSED,
                Job.closed_at < now - timedelta(days=closed_job_days),
            )
        )
        jobs = _rowcount(result)
    if job_change_days is not None:
        result = await session.execute(
            delete(JobChange).where(JobChange.changed_at < now - timedelta(days=job_change_days))
        )
        changes = _rowcount(result)
    if fetch_run_days is not None:
        latest_success = (
            select(func.max(FetchRun.id))
            .where(FetchRun.status == FetchStatus.SUCCESS)
            .group_by(FetchRun.source_id)
        )
        result = await session.execute(
            delete(FetchRun).where(
                and_(
                    FetchRun.started_at < now - timedelta(days=fetch_run_days),
                    FetchRun.status != FetchStatus.RUNNING,
                    FetchRun.id.not_in(latest_success),
                )
            )
        )
        runs = _rowcount(result)
    await session.commit()
    return PruneResult(jobs, changes, runs)


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)
