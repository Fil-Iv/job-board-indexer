"""Database engine and session helpers.

The same ``postgresql+psycopg://`` URL works for the async engine used by the app and
the sync engine used by Alembic (psycopg 3 supports both). ``postgres://`` and
``postgresql://`` URLs (as copied from the Supabase dashboard) are accepted too.

Connection poolers (Supabase Supavisor, PgBouncer) in transaction mode hand each
transaction to a different server connection, so server-side prepared statements break
there. :func:`connect_args` turns them off for pooler URLs (psycopg:
``prepare_threshold=None``; asyncpg: ``statement_cache_size=0``).
"""

from typing import Any

from sqlalchemy import make_url, pool
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobindex.config import Settings, get_settings

POOLER_PORT = 6543  # Supabase / PgBouncer transaction pooler


def normalize_url(url: str) -> URL:
    """``postgres://`` / ``postgresql://`` -> ``postgresql+psycopg://``; other drivers kept."""
    parsed = make_url(url)
    if parsed.drivername in {"postgres", "postgresql"}:
        parsed = parsed.set(drivername="postgresql+psycopg")
    return parsed


def is_pooler(url: URL) -> bool:
    host = (url.host or "").lower()
    return (
        host.endswith(".pooler.supabase.com")
        or url.port == POOLER_PORT
        or str(url.query.get("pgbouncer", "")).lower() == "true"
    )


def connect_args(url: URL, settings: Settings | None = None) -> dict[str, Any]:
    """Driver arguments; disables prepared statements behind a pooler (or when
    ``DATABASE_PREPARED_STATEMENTS=false``)."""
    settings = settings or get_settings()
    prepared = settings.database_prepared_statements
    if prepared is None:
        prepared = not is_pooler(url)
    if prepared:
        return {}
    if url.get_driver_name() == "asyncpg":
        return {"statement_cache_size": 0, "prepared_statement_cache_size": 0}
    return {"prepare_threshold": None}  # psycopg 3: never prepare


def _without_pooler_flag(url: URL) -> URL:
    # ``pgbouncer=true`` is a marker for us, not a libpq option.
    return url.difference_update_query(["pgbouncer"])


def create_engine(url: str | None = None) -> AsyncEngine:
    """Async engine whose pool fits ``CRAWL_CONCURRENCY`` crawls (one connection each
    while it writes) plus dedup, the API and checkpoint writes."""
    settings = get_settings()
    parsed = normalize_url(url or settings.database_url)
    options: dict[str, Any] = {"pool_pre_ping": True}
    if settings.database_null_pool:
        options["poolclass"] = pool.NullPool
    else:
        options["pool_size"] = settings.database_pool_size or max(5, settings.crawl_concurrency + 2)
        options["max_overflow"] = settings.database_max_overflow
        options["pool_timeout"] = settings.database_pool_timeout_seconds
    if settings.database_read_only:
        options["execution_options"] = {"postgresql_readonly": True}
    return create_async_engine(
        _without_pooler_flag(parsed),
        connect_args=connect_args(parsed, settings),
        **options,
    )


def create_sync_engine_args(url: str) -> tuple[URL, dict[str, Any]]:
    """URL and ``connect_args`` for a synchronous engine (Alembic)."""
    parsed = normalize_url(url)
    return _without_pooler_flag(parsed), connect_args(parsed)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
