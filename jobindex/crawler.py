"""Crawl a source and reconcile the result with the database.

``crawl_source(source_id)``:

1. Creates a ``fetch_runs`` row (status ``running``).
2. Fetches the complete list of open postings via the source's connector, plus detail
   pages for platforms that need them (only for new/changed postings).
3. **On any failure** — HTTP, robots.txt, unexpected payload, database error — marks the
   run ``failed``, sets ``sources.last_failure_at`` / ``last_error``, increments
   ``consecutive_failures`` and **touches no job**. A failed fetch never means "closed".
4. **On success** upserts postings:
   * new posting -> inserted;
   * ``content_hash`` changed -> updated, one ``job_changes`` row per changed meaningful
     field (see ``normalize.HASH_FIELDS``);
   * hash unchanged -> only cosmetic fields (URLs, HTML, ``posted_at``) refreshed silently;
   * previously closed posting seen again -> reopened (recorded as a status change);
   * every job_changes row carries ``parser_version`` and a ``cause``: ``upstream`` (the
     source data changed), ``parser`` (same source data, newer normalize.PARSER_VERSION)
     or ``local`` (our own metadata, e.g. ``set-company``);
   * open job missing from the result -> closed (recorded as a status change).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.connectors.base import BaseConnector, RawJob
from jobindex.connectors.registry import Registry, default_registry
from jobindex.db import create_engine, create_session_factory
from jobindex.http import HttpClient
from jobindex.models import ChangeCause, FetchRun, FetchStatus, Job, JobChange, JobStatus, Source
from jobindex.models.base import utcnow
from jobindex.normalize import HASH_FIELDS, PARSER_VERSION

log = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 4000


@dataclass(frozen=True, slots=True)
class CrawlResult:
    source_id: int
    fetch_run_id: int
    status: FetchStatus
    jobs_found: int = 0
    jobs_new: int = 0
    jobs_updated: int = 0
    jobs_closed: int = 0
    error: str | None = None


@asynccontextmanager
async def _default_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine()
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


async def crawl_source(
    source_id: int,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    http: HttpClient | None = None,
    registry: Registry | None = None,
) -> CrawlResult:
    """Crawl one source. Dependencies default to ones built from settings."""
    if session_factory is None:
        async with _default_session_factory() as factory:
            return await crawl_source(
                source_id, session_factory=factory, http=http, registry=registry
            )
    if http is None:
        async with HttpClient.from_settings() as client:
            return await crawl_source(
                source_id, session_factory=session_factory, http=client, registry=registry
            )
    async with session_factory() as session:
        return await _crawl(session, source_id, http, registry or default_registry())


async def crawl_sources(
    source_ids: list[int],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    http: HttpClient,
    concurrency: int = 4,
    registry: Registry | None = None,
) -> list[CrawlResult]:
    """Crawl several sources concurrently; per-host rate limits still apply via ``http``."""
    semaphore = asyncio.Semaphore(concurrency)

    async def one(source_id: int) -> CrawlResult:
        async with semaphore:
            return await crawl_source(
                source_id, session_factory=session_factory, http=http, registry=registry
            )

    return list(await asyncio.gather(*(one(sid) for sid in source_ids)))


async def active_source_ids(session: AsyncSession) -> list[int]:
    result = await session.scalars(
        select(Source.id).where(Source.active.is_(True)).order_by(Source.id)
    )
    return list(result.all())


# ---------------------------------------------------------------------------


async def _crawl(
    session: AsyncSession, source_id: int, http: HttpClient, registry: Registry
) -> CrawlResult:
    source = await session.get(Source, source_id)
    if source is None:
        raise LookupError(f"source {source_id} does not exist")

    run = FetchRun(source_id=source.id, started_at=utcnow(), status=FetchStatus.RUNNING)
    session.add(run)
    await session.commit()  # make the running attempt visible immediately
    run_id = run.id  # plain int: still usable after a rollback expires ``run``

    try:
        existing = {
            job.external_id: job
            for job in (await session.scalars(select(Job).where(Job.source_id == source_id))).all()
        }
        await session.commit()  # don't hold a transaction open during a slow fetch
        connector = registry.get(source.platform)(http)
        listed = await connector.fetch_jobs(source.board_identifier)
        if connector.needs_detail:
            raw_jobs = await _with_details(connector, source.board_identifier, listed, existing)
        else:
            raw_jobs = [(raw, raw.raw_hash()) for raw in listed]
        postings = [
            (raw.normalized(company_fallback=source.company_name), raw_hash)
            for raw, raw_hash in raw_jobs
        ]
    except Exception as exc:  # any failure is recorded, never interpreted as "no jobs"
        await session.rollback()
        return await _record_failure(session, source_id, run_id, exc)

    try:
        counts = _reconcile(session, source_id, postings, existing)
        now = utcnow()
        run.status = FetchStatus.SUCCESS
        run.finished_at = now
        run.jobs_found, run.jobs_new, run.jobs_updated, run.jobs_closed = counts
        source.last_success_at = now
        source.consecutive_failures = 0
        source.last_error = None
        await session.commit()
    except Exception as exc:  # e.g. a posting violating a DB constraint
        await session.rollback()
        return await _record_failure(session, source_id, run_id, exc)

    log.info("crawled source %s: found=%d new=%d updated=%d closed=%d", source_id, *counts)
    return CrawlResult(source_id, run_id, FetchStatus.SUCCESS, *counts)


async def _with_details(
    connector: BaseConnector,
    board_identifier: str,
    raw_jobs: list[RawJob],
    existing: dict[str, Job],
) -> list[tuple[RawJob, str]]:
    """Fetch detail pages only for new postings or ones whose list-level fields changed;
    otherwise reuse the stored detail fields.

    Returns ``(raw job, raw hash)`` pairs. A reused posting keeps its stored raw hash: its
    list fields are unchanged and its detail was not re-fetched, so nothing upstream is
    known to have changed.
    """
    result: list[tuple[RawJob, str]] = []
    for raw in raw_jobs:
        job = existing.get(raw.external_id)
        if (
            job is not None
            and job.description_html is not None
            and all(getattr(job, f) == getattr(raw, f) for f in connector.detail_cache_key)
        ):
            reused = replace(raw, **{f: getattr(job, f) for f in connector.detail_fields})
            result.append((reused, job.raw_hash or reused.raw_hash()))
            continue
        detailed = await connector.fetch_job_detail(board_identifier, raw)
        if detailed is not None:  # None = posting vanished between list and detail
            result.append((detailed, detailed.raw_hash()))
    return result


async def _record_failure(
    session: AsyncSession, source_id: int, run_id: int, exc: Exception
) -> CrawlResult:
    """Mark the run failed and bump the source's failure counters. Jobs are not touched."""
    error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
    log.warning("crawl of source %s failed: %s", source_id, error)
    run = await session.get(FetchRun, run_id)
    source = await session.get(Source, source_id)
    if run is None or source is None:  # pragma: no cover - deleted mid-crawl
        raise LookupError(f"fetch run {run_id} or source {source_id} disappeared")
    now = utcnow()
    run.status = FetchStatus.FAILED
    run.finished_at = now
    run.error = error
    source.last_failure_at = now
    source.consecutive_failures += 1
    source.last_error = error
    await session.commit()
    return CrawlResult(source_id, run_id, FetchStatus.FAILED, error=error)


def _reconcile(
    session: AsyncSession,
    source_id: int,
    postings: list[tuple[RawJob, str]],
    existing: dict[str, Job],
) -> tuple[int, int, int, int]:
    now = utcnow()
    # De-duplicate by external_id (last one wins) — some APIs repeat postings across pages.
    by_external_id = {p.external_id: (p, raw_hash) for p, raw_hash in postings}

    new = updated = closed = 0
    for external_id, (posting, raw_hash) in by_external_id.items():
        fields = posting.as_dict()
        fields.pop("external_id")
        digest = posting.content_hash()
        job = existing.get(external_id)

        if job is None:
            session.add(
                Job(
                    source_id=source_id,
                    external_id=external_id,
                    **fields,
                    content_hash=digest,
                    raw_hash=raw_hash,
                    parser_version=PARSER_VERSION,
                    status=JobStatus.OPEN,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_checked_at=now,
                )
            )
            new += 1
            continue

        changes: list[JobChange] = []
        if job.status is JobStatus.CLOSED:
            changes.append(_change(job, "status", JobStatus.CLOSED, JobStatus.OPEN, now))
            job.status = JobStatus.OPEN
            job.closed_at = None
        if job.content_hash != digest:
            cause = _cause(job, raw_hash)
            for name in HASH_FIELDS:
                if getattr(job, name) != fields[name]:
                    changes.append(_change(job, name, getattr(job, name), fields[name], now, cause))
            job.content_hash = digest
        job.raw_hash = raw_hash
        job.parser_version = PARSER_VERSION
        for name, value in fields.items():  # cosmetic fields are refreshed silently
            if getattr(job, name) != value:
                setattr(job, name, value)
        if changes:
            session.add_all(changes)
            updated += 1
        job.last_seen_at = now
        job.last_checked_at = now

    for external_id, job in existing.items():
        if external_id in by_external_id:
            continue
        job.last_checked_at = now
        if job.status is JobStatus.OPEN:
            session.add(_change(job, "status", JobStatus.OPEN, JobStatus.CLOSED, now))
            job.status = JobStatus.CLOSED
            job.closed_at = now
            closed += 1

    return len(by_external_id), new, updated, closed


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(getattr(value, "value", value))


def _cause(job: Job, raw_hash: str) -> ChangeCause:
    """Attribute a content change: the source (raw data differs), our parser (same raw
    data, newer PARSER_VERSION) or our own metadata (neither, e.g. a new company name).

    Rows from before raw hashes existed count as upstream: we cannot tell.
    """
    if job.raw_hash is None or job.raw_hash != raw_hash:
        return ChangeCause.UPSTREAM
    if job.parser_version != PARSER_VERSION:
        return ChangeCause.PARSER
    return ChangeCause.LOCAL


def _change(
    job: Job,
    field: str,
    old: Any,
    new: Any,
    at: datetime,
    cause: ChangeCause = ChangeCause.UPSTREAM,
) -> JobChange:
    return JobChange(
        job_id=job.id,
        changed_at=at,
        field=field,
        old_value=_as_text(old),
        new_value=_as_text(new),
        parser_version=PARSER_VERSION,
        cause=cause,
    )
