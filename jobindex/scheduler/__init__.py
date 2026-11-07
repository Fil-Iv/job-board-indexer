"""Periodic crawling with APScheduler (runs inside the API process when enabled)."""

import logging
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobindex.config import Settings
from jobindex.crawler import active_source_ids, crawl_sources
from jobindex.http import HttpClient

log = logging.getLogger(__name__)


async def crawl_all_active(
    session_factory: async_sessionmaker[AsyncSession], http: HttpClient, concurrency: int
) -> None:
    async with session_factory() as session:
        source_ids = await active_source_ids(session)
    log.info("scheduled crawl of %d active sources", len(source_ids))
    await crawl_sources(
        source_ids, session_factory=session_factory, http=http, concurrency=concurrency
    )


def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession], http: HttpClient, settings: Settings
) -> Any:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        crawl_all_active,
        "interval",
        minutes=settings.crawl_interval_minutes,
        args=[session_factory, http, settings.crawl_concurrency],
        id="crawl_all_active",
        max_instances=1,  # never overlap two full crawls
        coalesce=True,
    )
    return scheduler
