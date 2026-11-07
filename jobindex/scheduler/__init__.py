"""Continuous crawling: ``python -m jobindex.cli schedule`` (the ``worker`` container).

An APScheduler job ticks every ``SCHEDULER_TICK_SECONDS``. Each tick:

1. reads every active source with the start time of its latest crawl;
2. picks the sources that are **due**: never crawled, or last started longer ago than
   their platform's interval (``CRAWL_PLATFORM_INTERVALS``, default
   ``CRAWL_INTERVAL_MINUTES``) plus a per-source **jitter** (so boards added together
   don't stay in lock-step) and a **backoff** for sources that keep failing;
3. **skips** sources already being crawled — by this process, or by another one (a
   ``fetch_runs`` row still ``running`` and younger than ``CRAWL_STALE_AFTER_HOURS``);
4. starts the crawls in the background; at most ``CRAWL_CONCURRENCY`` run at once
   (per-host rate limits are enforced separately by ``HttpClient``).

After crawls succeed, dedup runs (coalesced: one run at a time, re-run once if more
crawls finished meanwhile). A failed crawl is only recorded (``crawler.crawl_source``);
it never closes jobs.

Every tick touches ``SCHEDULER_HEARTBEAT_FILE``; the container healthcheck reads its age.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.config import Settings
from jobindex.connectors.registry import Registry
from jobindex.crawler import CrawlResult, crawl_source
from jobindex.dedup import run_dedup
from jobindex.http import HttpClient
from jobindex.models import FetchRun, FetchStatus, Source
from jobindex.models.base import utcnow

log = logging.getLogger(__name__)

MAX_BACKOFF_FACTOR = 8


@dataclass(frozen=True, slots=True)
class SourceState:
    id: int
    platform: str
    consecutive_failures: int
    last_started_at: datetime | None
    running_elsewhere: bool


def jitter_fraction(source_id: int, last_started_at: datetime) -> float:
    """Deterministic value in [0, 1) per source and run (stable across ticks)."""
    digest = hashlib.sha256(f"{source_id}:{last_started_at.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def next_due(state: SourceState, interval: timedelta, jitter: float = 0.1) -> datetime | None:
    """When the source should be crawled next; ``None`` = now (never crawled).

    ``interval`` is multiplied by a backoff for repeated failures (1, 2, 4, then 8 times
    for 1, 2, 3, 4+ consecutive failures), then up to ``jitter`` of it is added.
    """
    if state.last_started_at is None:
        return None
    backoff: int = min(2 ** max(state.consecutive_failures - 1, 0), MAX_BACKOFF_FACTOR)
    wait: timedelta = interval * backoff
    wait += wait * jitter * jitter_fraction(state.id, state.last_started_at)
    return state.last_started_at + wait


class CrawlScheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        http: HttpClient,
        settings: Settings,
        registry: Registry | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.http = http
        self.settings = settings
        self.registry = registry
        self.running: dict[int, asyncio.Task[CrawlResult | None]] = {}
        self._slots = asyncio.Semaphore(settings.crawl_concurrency)
        self._dedup_task: asyncio.Task[None] | None = None
        self._dedup_pending = False
        self._scheduler: Any = None

    # -- scheduling ---------------------------------------------------------

    def interval_for(self, platform: str) -> timedelta:
        minutes = self.settings.crawl_platform_intervals.get(
            platform, self.settings.crawl_interval_minutes
        )
        return timedelta(minutes=minutes)

    @property
    def stale_after(self) -> timedelta:
        return timedelta(hours=self.settings.crawl_stale_after_hours)

    async def load_states(self, now: datetime) -> list[SourceState]:
        last_run = (
            select(FetchRun.source_id, func.max(FetchRun.started_at).label("started"))
            .group_by(FetchRun.source_id)
            .subquery()
        )
        running = (
            select(FetchRun.source_id)
            .where(
                FetchRun.status == FetchStatus.RUNNING,
                FetchRun.started_at > now - self.stale_after,
            )
            .distinct()
            .subquery()
        )
        stmt = (
            select(
                Source.id,
                Source.platform,
                Source.consecutive_failures,
                last_run.c.started,
                running.c.source_id.is_not(None),
            )
            .outerjoin(last_run, last_run.c.source_id == Source.id)
            .outerjoin(running, running.c.source_id == Source.id)
            .where(Source.active.is_(True))
            .order_by(Source.id)
        )
        async with self.session_factory() as session:
            rows = await session.execute(stmt)
            return [SourceState(*row) for row in rows]

    def due(self, states: list[SourceState], now: datetime) -> list[SourceState]:
        """Sources to start now, longest-waiting first (never-crawled ones first)."""
        due = []
        for state in states:
            if state.id in self.running or state.running_elsewhere:
                continue
            at = next_due(state, self.interval_for(state.platform), self.settings.crawl_jitter)
            if at is None or at <= now:
                due.append((at or datetime.min.replace(tzinfo=now.tzinfo), state))
        return [state for _, state in sorted(due, key=lambda d: (d[0], d[1].id))]

    async def tick(self) -> list[int]:
        """Start crawls for every due source; returns their IDs."""
        now = utcnow()
        self._heartbeat()
        started = []
        for state in self.due(await self.load_states(now), now):
            self.running[state.id] = asyncio.create_task(self._crawl(state.id))
            started.append(state.id)
        if started:
            log.info("scheduler: queued %d sources %s", len(started), started)
        return started

    async def _crawl(self, source_id: int) -> CrawlResult | None:
        try:
            async with self._slots:
                result = await crawl_source(
                    source_id,
                    session_factory=self.session_factory,
                    http=self.http,
                    registry=self.registry,
                )
            if result.status is FetchStatus.SUCCESS:
                self.request_dedup()
            return result
        except Exception:  # crawl_source records its own failures; this is a last resort
            log.exception("scheduler: crawl of source %d crashed", source_id)
            return None
        finally:
            self.running.pop(source_id, None)

    # -- dedup ------------------------------------------------------------

    def request_dedup(self) -> None:
        if self._dedup_task is not None and not self._dedup_task.done():
            self._dedup_pending = True
            return
        self._dedup_task = asyncio.create_task(self._dedup_loop())

    async def _dedup_loop(self) -> None:
        window = timedelta(days=self.settings.dedup_date_window_days)
        while True:
            self._dedup_pending = False
            try:
                async with self.session_factory() as session:
                    await run_dedup(session, window=window)
            except Exception:
                log.exception("scheduler: dedup failed")
            if not self._dedup_pending:
                return

    # -- lifecycle --------------------------------------------------------

    async def recover_abandoned_runs(self) -> int:
        """Mark ``running`` rows older than ``CRAWL_STALE_AFTER_HOURS`` as failed (their
        process died). Jobs are not touched."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(FetchRun)
                .where(
                    FetchRun.status == FetchStatus.RUNNING,
                    FetchRun.started_at <= utcnow() - self.stale_after,
                )
                .values(
                    status=FetchStatus.FAILED,
                    finished_at=utcnow(),
                    error="abandoned: the crawling process stopped before finishing",
                )
            )
            await session.commit()
        count: int = getattr(result, "rowcount", 0) or 0
        if count:
            log.warning("scheduler: marked %d abandoned runs as failed", count)
        return count

    async def wait_idle(self) -> None:
        """Wait for running crawls and dedup (used by tests and shutdown)."""
        while self.running or (self._dedup_task and not self._dedup_task.done()):
            await asyncio.gather(
                *self.running.values(), *([self._dedup_task] if self._dedup_task else [])
            )

    def start(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._scheduler.add_job(
            self.tick,
            "interval",
            seconds=self.settings.scheduler_tick_seconds,
            next_run_time=utcnow(),
            id="tick",
            max_instances=1,
            coalesce=True,
        )
        self._scheduler.start()

    async def stop(self, grace_seconds: float = 30.0) -> None:
        """Stop ticking; give running crawls ``grace_seconds``, then cancel them (the
        crawler records a cancelled run as failed; jobs are not touched)."""
        if self._scheduler is not None:
            self._scheduler.shutdown(wait=False)
        tasks = list(self.running.values())
        if tasks:
            log.info("scheduler: waiting for %d running crawls", len(tasks))
            _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def _heartbeat(self) -> None:
        path = self.settings.scheduler_heartbeat_file
        if path:
            try:
                Path(path).touch()
            except OSError:
                log.warning("scheduler: cannot write heartbeat file %s", path)
