"""Crawl scheduler: due computation, per-platform intervals, skip-if-running, failures."""

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.config import Settings
from jobindex.connectors.base import RawJob
from jobindex.connectors.registry import Registry
from jobindex.http import FetchError
from jobindex.models import FetchRun, FetchStatus, Job, JobStatus, Source
from jobindex.models.base import utcnow
from jobindex.scheduler import CrawlScheduler, SourceState, next_due
from tests.fakes import FakeConnector, mock_http

Factory = async_sessionmaker[AsyncSession]
T0 = datetime(2026, 11, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def state(failures: int = 0, last: datetime | None = T0) -> SourceState:
    return SourceState(1, "fake", failures, last, running_elsewhere=False)


def test_next_due_interval_jitter_and_backoff() -> None:
    assert next_due(state(last=None), HOUR) is None  # never crawled: due now
    assert next_due(state(), HOUR, jitter=0) == T0 + HOUR
    due = next_due(state(), HOUR, jitter=0.1)
    assert due is not None
    assert T0 + HOUR <= due < T0 + HOUR * 1.1
    assert next_due(state(), HOUR, jitter=0.1) == due  # stable between ticks
    assert next_due(state(failures=1), HOUR, jitter=0) == T0 + HOUR
    assert next_due(state(failures=3), HOUR, jitter=0) == T0 + 4 * HOUR
    assert next_due(state(failures=50), HOUR, jitter=0) == T0 + 8 * HOUR


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "crawl_interval_minutes": 60,
        "crawl_platform_intervals": {},
        "crawl_jitter": 0.0,
        "crawl_concurrency": 2,
        "scheduler_heartbeat_file": "",
    }
    return Settings(**(defaults | overrides))  # type: ignore[arg-type]


async def _source(session: AsyncSession, board: str, platform: str = "fake") -> int:
    source = Source(platform=platform, board_identifier=board, base_url=f"https://x/{board}")
    session.add(source)
    await session.commit()
    return source.id


async def _run(session: AsyncSession, source_id: int, ago: timedelta, status: FetchStatus) -> None:
    started = utcnow() - ago
    session.add(FetchRun(source_id=source_id, started_at=started, status=status))
    await session.commit()


def _scheduler(factory: Factory, registry: Registry, **settings: object) -> CrawlScheduler:
    http, _ = mock_http({})
    return CrawlScheduler(factory, http, _settings(**settings), registry)


async def test_tick_crawls_due_sources_only(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    never = await _source(session, "never")
    stale = await _source(session, "stale")
    fresh = await _source(session, "fresh")
    slow = await _source(session, "slow", platform="fake-detail")
    elsewhere = await _source(session, "elsewhere")
    inactive = await _source(session, "inactive")
    await _run(session, stale, 2 * HOUR, FetchStatus.SUCCESS)
    await _run(session, fresh, timedelta(minutes=10), FetchStatus.SUCCESS)
    await _run(session, slow, 2 * HOUR, FetchStatus.SUCCESS)  # platform interval: 4 h
    await _run(session, elsewhere, timedelta(minutes=90), FetchStatus.RUNNING)  # CLI crawl
    (await session.get(Source, inactive)).active = False  # type: ignore[union-attr]
    await session.commit()

    FakeConnector.result = [RawJob(external_id="1", title="Engineer")]
    scheduler = _scheduler(
        session_factory, fake_registry, crawl_platform_intervals={"fake-detail": 240}
    )
    assert await scheduler.tick() == [never, stale]
    assert await scheduler.tick() == []  # already running in this process: skipped
    await scheduler.wait_idle()

    runs = await session.scalars(
        select(FetchRun.source_id).where(FetchRun.status == FetchStatus.SUCCESS)
    )
    assert never in runs.all()
    assert await scheduler.tick() == []  # just crawled: not due again
    # dedup ran after the successful crawls
    assert await session.scalar(select(Job.canonical_job_id)) is not None


async def test_concurrency_limit(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    for board in "abcd":
        await _source(session, board)
    active = 0
    peak = 0

    original = FakeConnector.fetch_jobs

    async def slow_fetch(self: FakeConnector, board_identifier: str) -> list[RawJob]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return []

    FakeConnector.fetch_jobs = slow_fetch  # type: ignore[method-assign]
    try:
        scheduler = _scheduler(session_factory, fake_registry, crawl_concurrency=2)
        assert len(await scheduler.tick()) == 4
        await scheduler.wait_idle()
    finally:
        FakeConnector.fetch_jobs = original  # type: ignore[method-assign]
    assert peak == 2


async def test_failed_scheduled_crawl_keeps_jobs_open(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _source(session, "acme")
    scheduler = _scheduler(session_factory, fake_registry, crawl_interval_minutes=0)
    FakeConnector.result = [RawJob(external_id="1", title="A"), RawJob(external_id="2", title="B")]
    await scheduler.tick()
    await scheduler.wait_idle()

    FakeConnector.result = FetchError("HTTP 503", url="https://x", status_code=503)
    assert await scheduler.tick() == [source_id]
    await scheduler.wait_idle()

    statuses = (await session.scalars(select(Job.status))).all()
    assert statuses == [JobStatus.OPEN, JobStatus.OPEN]
    source = await session.get(Source, source_id)
    assert source is not None
    await session.refresh(source)
    assert source.consecutive_failures == 1


async def test_abandoned_runs_are_recovered_as_failed(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _source(session, "acme")
    await _run(session, source_id, timedelta(hours=13), FetchStatus.RUNNING)
    await _run(session, source_id, timedelta(hours=1), FetchStatus.RUNNING)
    scheduler = _scheduler(session_factory, fake_registry)
    assert await scheduler.recover_abandoned_runs() == 1
    statuses = (await session.scalars(select(FetchRun.status).order_by(FetchRun.id))).all()
    assert statuses == [FetchStatus.FAILED, FetchStatus.RUNNING]


async def test_cancelled_crawl_is_recorded_as_failure(
    session: AsyncSession, session_factory: Factory, fake_registry: Registry
) -> None:
    source_id = await _source(session, "acme")
    original = FakeConnector.fetch_jobs

    async def hang(self: FakeConnector, board_identifier: str) -> list[RawJob]:
        await asyncio.sleep(10)
        return []

    FakeConnector.fetch_jobs = hang  # type: ignore[method-assign]
    try:
        scheduler = _scheduler(session_factory, fake_registry)
        await scheduler.tick()
        await asyncio.sleep(0.05)  # let the crawl start
        await scheduler.stop(grace_seconds=0.01)
    finally:
        FakeConnector.fetch_jobs = original  # type: ignore[method-assign]
    run = await session.scalar(select(FetchRun).where(FetchRun.source_id == source_id))
    assert run is not None
    assert run.status is FetchStatus.FAILED
    assert run.error == "CrawlCancelledError: crawl cancelled"
