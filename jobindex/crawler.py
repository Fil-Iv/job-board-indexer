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
   * open job missing from the result -> closed (recorded as a status change);
   * posting whose ``expires_at`` has passed -> stored/kept **closed** (never counted as
     open), so platforms that keep expired pages listed don't re-fetch them every crawl.
5. **Size budget** (``MAX_JOBS_PER_SOURCE``, ``DESCRIPTION_HTML_MAX_CHARS``,
   ``JOB_CHANGE_VALUE_MAX_CHARS``): the full list is always fetched; at most N postings
   are kept (stored ones first, so a cap never closes anything: only postings missing
   from the full list are closed); oversized HTML and change values are trimmed.
6. **Safety**: connectors that opt in (``max_job_drop()``) fail the run instead of
   closing jobs when the open-job count drops by more than that fraction vs. the last
   successful run (a broken sitemap or page template looks like mass closures).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.checkpoint import SourceCheckpoint
from jobindex.config import get_settings
from jobindex.connectors.base import BaseConnector, JobCountDropError, RawJob
from jobindex.connectors.registry import Registry, default_registry
from jobindex.db import create_engine, create_session_factory
from jobindex.http import HttpClient
from jobindex.models import ChangeCause, FetchRun, FetchStatus, Job, JobChange, JobStatus, Source
from jobindex.models.base import utcnow
from jobindex.normalize import HASH_FIELDS, PARSER_VERSION

log = logging.getLogger(__name__)

DETAIL_PROGRESS_EVERY = 100  # log detail-fetch progress for large boards

MAX_ERROR_LENGTH = 4000

DEADLINE_ERROR = "deadline reached: cancelled or not started (resumes on the next crawl)"


@dataclass(frozen=True, slots=True)
class CrawlResult:
    source_id: int
    fetch_run_id: int | None  # None: crashed before its fetch_runs row was written
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
    deadline_seconds: float | None = None,
) -> list[CrawlResult]:
    """Crawl several sources concurrently; per-host rate limits still apply via ``http``.

    One source crashing (e.g. the database is briefly unreachable before its run row
    exists) is reported as a failed result; it does not cancel the other crawls.

    ``deadline_seconds``: crawls still running then are cancelled (recorded as failed
    runs; resumable connectors keep their checkpoint, so the next crawl continues where
    this one stopped) and crawls not started yet are skipped. Both are returned as
    failed results with :data:`DEADLINE_ERROR`. Results keep the order of ``source_ids``.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def one(source_id: int) -> CrawlResult:
        async with semaphore:
            try:
                return await crawl_source(
                    source_id, session_factory=session_factory, http=http, registry=registry
                )
            except Exception as exc:
                log.exception("crawl of source %s crashed", source_id)
                error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
                return CrawlResult(source_id, None, FetchStatus.FAILED, error=error)

    tasks = [asyncio.create_task(one(sid)) for sid in source_ids]
    if not tasks:
        return []
    _, pending = await asyncio.wait(tasks, timeout=deadline_seconds)
    for task in pending:
        task.cancel()
    if pending:
        log.warning("crawl deadline reached: cancelling %d unfinished crawls", len(pending))
        await asyncio.wait(pending)
    return [
        CrawlResult(sid, None, FetchStatus.FAILED, error=DEADLINE_ERROR)
        if task.cancelled()
        else task.result()
        for sid, task in zip(source_ids, tasks, strict=True)
    ]


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
        previous = await _last_success_count(session, source_id)
        # Don't hold a transaction (and a pooled connection) open during a slow fetch.
        await session.commit()
        connector = registry.get(source.platform)(http, SourceCheckpoint(session, source))
        listed = await connector.fetch_jobs(source.board_identifier)
        # Early check (list level, before slow detail fetches); the open count can only
        # be lower than this.
        listed_ids = {raw.external_id for raw in listed}
        _check_drop(connector, previous, len(listed_ids))
        listed = cap_postings(listed, existing, get_settings().max_jobs_per_source)
        if connector.needs_detail:
            raw_jobs = await _with_details(connector, source.board_identifier, listed, existing)
        else:
            raw_jobs = [(raw, raw.raw_hash()) for raw in listed]
        if source.company_name is None and any(raw.company is None for raw, _ in raw_jobs):
            source.company_name = await _company_name(connector, source.board_identifier)
        html_limit = get_settings().description_html_max_chars
        postings = [
            (trim_html(raw.normalized(company_fallback=source.company_name), html_limit), raw_hash)
            for raw, raw_hash in raw_jobs
        ]
        now = utcnow()
        _check_drop(
            connector, previous, len({p.external_id for p, _ in postings if _is_open(p, now)})
        )
    except Exception as exc:  # any failure is recorded, never interpreted as "no jobs"
        await session.rollback()
        return await _record_failure(session, source_id, run_id, exc)
    except asyncio.CancelledError:  # worker shutdown: record it, then keep cancelling
        await asyncio.shield(_record_cancel(session, source_id, run_id))
        raise

    try:
        counts = _reconcile(session, source_id, postings, existing, listed_ids)
        now = utcnow()
        run.status = FetchStatus.SUCCESS
        run.finished_at = now
        run.jobs_found, run.jobs_new, run.jobs_updated, run.jobs_closed = counts
        source.last_success_at = now
        source.consecutive_failures = 0
        source.last_error = None
        source.crawl_checkpoint = None
        await session.commit()
    except Exception as exc:  # e.g. a posting violating a DB constraint
        await session.rollback()
        return await _record_failure(session, source_id, run_id, exc)

    log.info("crawled source %s: found=%d new=%d updated=%d closed=%d", source_id, *counts)
    return CrawlResult(source_id, run_id, FetchStatus.SUCCESS, *counts)


def cap_postings(listed: list[RawJob], existing: dict[str, Job], limit: int) -> list[RawJob]:
    """At most ``limit`` distinct postings (0 = all): stored open ones first (so the
    stored set stays stable), then new ones, then stored closed ones; newest first."""
    ids = list(dict.fromkeys(raw.external_id for raw in listed))
    if limit <= 0 or len(ids) <= limit:
        return listed
    first: dict[str, RawJob] = {}
    for raw in listed:
        first.setdefault(raw.external_id, raw)

    def rank(external_id: str) -> tuple[int, float, str]:
        job = existing.get(external_id)
        stored = 2 if job is not None else 1
        if job is not None and job.status is JobStatus.OPEN:
            stored = 0
        posted = first[external_id].posted_at
        return stored, -(posted.timestamp() if posted else 0.0), external_id

    keep = set(sorted(ids, key=rank)[:limit])
    return [raw for raw in listed if raw.external_id in keep]


def trim_html(posting: RawJob, limit: int | None) -> RawJob:
    """Drop ``description_html`` longer than ``limit`` characters (stored as "", which
    still counts as "detail fetched" for detail connectors). The text is kept."""
    html = posting.description_html
    if limit is None or html is None or len(html) <= limit:
        return posting
    return replace(posting, description_html="")


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
    fetched = 0
    for done, raw in enumerate(raw_jobs, start=1):
        if done % DETAIL_PROGRESS_EVERY == 0:
            log.info(
                "%s %s: details %d/%d (%d fetched, rest reused)",
                connector.platform,
                board_identifier,
                done,
                len(raw_jobs),
                fetched,
            )
        job = existing.get(raw.external_id)
        if (
            job is not None
            # a stored detail (or an expired posting's tombstone) exists
            and (job.description_html is not None or job.expires_at is not None)
            and all(getattr(job, f) == getattr(raw, f) for f in connector.detail_cache_key)
        ):
            reused = replace(raw, **{f: getattr(job, f) for f in connector.detail_fields})
            result.append((reused, job.raw_hash or reused.raw_hash()))
            continue
        fetched += 1
        detailed = await connector.fetch_job_detail(board_identifier, raw)
        if detailed is not None:  # None = posting vanished between list and detail
            result.append((detailed, detailed.raw_hash()))
    return result


async def _company_name(connector: BaseConnector, board_identifier: str) -> str | None:
    """The board's employer name, for postings without one (one request; a failure only
    logs: it must not fail the crawl)."""
    try:
        return await connector.fetch_company_name(board_identifier)
    except Exception as exc:
        log.warning("company name of %s %s: %s", connector.platform, board_identifier, exc)
        return None


async def _last_success_count(session: AsyncSession, source_id: int) -> int | None:
    """Open postings found by the source's last successful run (None if there was none)."""
    count: int | None = await session.scalar(
        select(FetchRun.jobs_found)
        .where(FetchRun.source_id == source_id, FetchRun.status == FetchStatus.SUCCESS)
        .order_by(desc(FetchRun.finished_at))
        .limit(1)
    )
    return count


def _check_drop(connector: BaseConnector, previous: int | None, current: int) -> None:
    max_drop = connector.max_job_drop()
    if max_drop is None or not previous:
        return
    if current < previous * (1 - max_drop):
        raise JobCountDropError(
            f"only {current} open postings vs {previous} on the last successful crawl "
            f"(more than {max_drop:.0%} fewer); refusing to close them. If the drop is "
            "real, raise the threshold (GENERIC_MAX_JOB_DROP) for one crawl."
        )


def _is_open(posting: RawJob, now: datetime) -> bool:
    return posting.expires_at is None or posting.expires_at > now


class CrawlCancelledError(Exception):
    """The crawl was cancelled (e.g. the worker is shutting down)."""


async def _record_cancel(session: AsyncSession, source_id: int, run_id: int) -> None:
    await session.rollback()
    await _record_failure(session, source_id, run_id, CrawlCancelledError("crawl cancelled"))


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
    listed_ids: set[str] | None = None,
) -> tuple[int, int, int, int]:
    """Apply a successful fetch. ``listed_ids``: every posting id of the full list (a
    superset of ``postings`` when ``MAX_JOBS_PER_SOURCE`` capped it); stored postings
    in it are still listed and are never closed."""
    now = utcnow()
    # De-duplicate by external_id (last one wins) — some APIs repeat postings across pages.
    by_external_id = {p.external_id: (p, raw_hash) for p, raw_hash in postings}

    new = updated = closed = 0
    for external_id, (posting, raw_hash) in by_external_id.items():
        fields = posting.as_dict()
        fields.pop("external_id")
        digest = posting.content_hash()
        job = existing.get(external_id)
        is_open = _is_open(posting, now)

        if job is None:
            # An already-expired posting is stored closed: it is never counted as open,
            # and the stored row lets detail-fetching connectors skip it next time.
            session.add(
                Job(
                    source_id=source_id,
                    external_id=external_id,
                    **fields,
                    content_hash=digest,
                    raw_hash=raw_hash,
                    parser_version=PARSER_VERSION,
                    status=JobStatus.OPEN if is_open else JobStatus.CLOSED,
                    first_seen_at=now,
                    last_seen_at=now,
                    last_checked_at=now,
                    closed_at=None if is_open else now,
                )
            )
            new += is_open
            continue

        changes: list[JobChange] = []
        if job.status is JobStatus.CLOSED and is_open:
            changes.append(_change(job, "status", JobStatus.CLOSED, JobStatus.OPEN, now))
            job.status = JobStatus.OPEN
            job.closed_at = None
        elif job.status is JobStatus.OPEN and not is_open:
            changes.append(_change(job, "status", JobStatus.OPEN, JobStatus.CLOSED, now))
            job.status = JobStatus.CLOSED
            job.closed_at = now
            closed += 1
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
            updated += is_open
        job.last_seen_at = now
        job.last_checked_at = now

    still_listed = listed_ids or set()
    for external_id, job in existing.items():
        if external_id in by_external_id or external_id in still_listed:
            continue
        job.last_checked_at = now
        if job.status is JobStatus.OPEN:
            session.add(_change(job, "status", JobStatus.OPEN, JobStatus.CLOSED, now))
            job.status = JobStatus.CLOSED
            job.closed_at = now
            closed += 1

    found = sum(_is_open(p, now) for p, _ in by_external_id.values())
    return found, new, updated, closed


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = value.isoformat() if isinstance(value, datetime) else str(getattr(value, "value", value))
    limit = get_settings().job_change_value_max_chars
    if limit is not None and len(text) > limit:
        return text[:limit] + "…"
    return text


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
