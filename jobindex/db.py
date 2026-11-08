"""Database engine and session helpers.

The same ``postgresql+psycopg://`` URL works for the async engine used by the app and
the sync engine used by Alembic (psycopg 3 supports both).
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobindex.config import get_settings


def create_engine(url: str | None = None) -> AsyncEngine:
    """Async engine whose pool fits ``CRAWL_CONCURRENCY`` crawls (one connection each
    while it writes) plus dedup, the API and checkpoint writes."""
    settings = get_settings()
    return create_async_engine(
        url or settings.database_url,
        pool_pre_ping=True,
        pool_size=max(5, settings.crawl_concurrency + 2),
        max_overflow=10,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
