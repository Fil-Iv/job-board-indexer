"""Shared fixtures.

Tests run against a real PostgreSQL database (``TEST_DATABASE_URL``, the
``jobindex_test`` database in docker-compose). The schema is built once per session by
running the Alembic migrations, and every table is truncated after each test.
"""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobindex.config import get_settings
from jobindex.connectors.registry import Registry
from jobindex.db import create_engine as create_async_engine
from jobindex.db import create_session_factory
from tests.fakes import FakeConnector, FakeDetailConnector

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("job_changes", "fetch_runs", "jobs", "sources", "pending_sources")


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.exit("TEST_DATABASE_URL is not set; run tests with `make test`", returncode=2)
    return url


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["database_url"] = database_url
    cfg.attributes["configure_logger"] = False
    return cfg


# Recreates the hosted (Supabase) setup in the test database, so the migrations are
# tested against it: Supabase's API roles with Supabase's default grants on everything
# new in `public`, and the website role from docs/DEPLOY.md A1 step 7 (NOLOGIN here).
SUPABASE_LIKE_ROLES_SQL = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated', 'jobindex_web'] LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', r);
    END IF;
  END LOOP;
END $$;
GRANT USAGE ON SCHEMA public TO anon, authenticated, jobindex_web;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON FUNCTIONS TO anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO jobindex_web;
"""


@pytest.fixture(scope="session")
def migrated_db(database_url: str) -> str:
    """Reset the test database and apply all migrations once per test session."""
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text(SUPABASE_LIKE_ROLES_SQL))
    engine.dispose()
    command.upgrade(alembic_config(database_url), "head")
    return database_url


@pytest.fixture(autouse=True)
def _clean_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if "migrated_db" not in request.fixturenames:
        return
    engine = create_engine(request.getfixturevalue("migrated_db"))
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE"))
    engine.dispose()


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    yield eng
    await eng.dispose()


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def settings_env(migrated_db: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point ``get_settings()`` (used by the CLI) at the test database."""
    monkeypatch.setenv("DATABASE_URL", migrated_db)
    monkeypatch.setenv("RATE_LIMIT_PER_HOST_SECONDS", "0")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_registry() -> Iterator[Registry]:
    def reset() -> None:
        FakeConnector.result = []
        FakeDetailConnector.result = []
        FakeDetailConnector.details = {}
        FakeDetailConnector.detail_calls = []

    reset()
    registry = Registry()
    registry.add(FakeConnector)
    registry.add(FakeDetailConnector)
    yield registry
    reset()
