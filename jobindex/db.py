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
    return create_async_engine(url or get_settings().database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
