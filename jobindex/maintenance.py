"""Database size report and retention (``db-size`` and ``prune`` CLI commands).

The hosted demo runs on the Supabase free tier (500 MB); see
docs/OPERATIONS.md#size-budget for how the limits below keep it under ~400 MB.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

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
            f"  {t.name:<20} {t.total_bytes / MB:8.1f} MB {t.rows:>10,} rows (est.)"
            for t in size.tables
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


async def recover_abandoned_runs(session: AsyncSession, older_than: timedelta) -> int:
    """Mark ``running`` fetch runs started more than ``older_than`` ago as failed: the
    process crawling them died (or a CI job was killed). Jobs are not touched."""
    result = await session.execute(
        update(FetchRun)
        .where(FetchRun.status == FetchStatus.RUNNING, FetchRun.started_at <= utcnow() - older_than)
        .values(
            status=FetchStatus.FAILED,
            finished_at=utcnow(),
            error="abandoned: the crawling process stopped before finishing",
        )
    )
    await session.commit()
    return _rowcount(result)


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


@dataclass(slots=True)
class CompactResult:
    jobs: int  # postings whose description was trimmed to the current budget
    job_changes: int  # history rows whose description values were dropped


async def compact_descriptions(
    session: AsyncSession,
    html_limit: int | None,
    text_limit: int | None,
    batch: int = 500,
) -> CompactResult:
    """Apply ``DESCRIPTION_HTML_MAX_CHARS`` / ``DESCRIPTION_TEXT_MAX_CHARS`` to postings
    stored before the limits were set (the crawler applies them to new data), and drop
    description values from ``job_changes``.

    The content hash is recomputed from the trimmed fields, exactly as the crawler hashes
    a trimmed posting, so the next crawl does not see every job as changed."""
    from jobindex.crawler import LONG_CHANGE_FIELDS, truncate_text
    from jobindex.normalize import HASH_FIELDS, content_hash

    too_long = []
    if html_limit is not None:
        too_long.append(func.length(Job.description_html) > html_limit)
    if text_limit is not None:
        too_long.append(func.length(Job.description_text) > text_limit + len("…"))
    jobs = 0
    if too_long:
        ids = list(await session.scalars(select(Job.id).where(or_(*too_long)).order_by(Job.id)))
        for start in range(0, len(ids), batch):
            chunk = ids[start : start + batch]
            for job in await session.scalars(select(Job).where(Job.id.in_(chunk))):
                html = job.description_html
                if html_limit is not None and html and len(html) > html_limit:
                    job.description_html = ""
                job.description_text = truncate_text(job.description_text, text_limit)
                job.content_hash = content_hash({f: getattr(job, f) for f in HASH_FIELDS})
                jobs += 1
            await session.commit()
    result = await session.execute(
        update(JobChange)
        .where(
            JobChange.field.in_(LONG_CHANGE_FIELDS),
            or_(JobChange.old_value.is_not(None), JobChange.new_value.is_not(None)),
        )
        .values(old_value=None, new_value=None)
    )
    await session.commit()
    return CompactResult(jobs, _rowcount(result))


async def vacuum_full(
    engine: AsyncEngine, tables: tuple[str, ...] = ("jobs", "job_changes")
) -> None:
    """Give the space of trimmed rows back to the operating system (Supabase counts the
    database's on-disk size). Locks each table for a moment; run only after compacting."""
    async with engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for table in tables:
            await auto.exec_driver_sql(f"VACUUM (FULL, ANALYZE) {table}")
