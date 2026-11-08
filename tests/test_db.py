"""Engine options for the Supabase pooler (docs/DEPLOY.md) and local Postgres."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from jobindex.config import Settings, get_settings
from jobindex.db import connect_args, create_engine, is_pooler, normalize_url

SESSION_POOLER = (
    "postgresql://postgres.abcd:pw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"
)
TRANSACTION_POOLER = SESSION_POOLER.replace(":5432/", ":6543/")


@pytest.mark.parametrize(
    ("url", "driver"),
    [
        ("postgres://u:p@h/db", "postgresql+psycopg"),
        ("postgresql://u:p@h/db", "postgresql+psycopg"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg"),
        ("postgresql+asyncpg://u:p@h/db", "postgresql+asyncpg"),
    ],
)
def test_normalize_url(url: str, driver: str) -> None:
    assert normalize_url(url).drivername == driver


@pytest.mark.parametrize(
    ("url", "pooler"),
    [
        (SESSION_POOLER, True),
        (TRANSACTION_POOLER, True),
        ("postgresql://u:p@pgbouncer.internal:6432/db?pgbouncer=true", True),
        ("postgresql://u:p@db.abcd.supabase.co:5432/postgres", False),  # direct connection
        ("postgresql+psycopg://jobindex:jobindex@db:5432/jobindex", False),
    ],
)
def test_is_pooler(url: str, pooler: bool) -> None:
    assert is_pooler(normalize_url(url)) is pooler


def test_prepared_statements_off_behind_pooler() -> None:
    auto = Settings()
    assert connect_args(normalize_url(TRANSACTION_POOLER), auto) == {"prepare_threshold": None}
    assert connect_args(normalize_url(SESSION_POOLER), auto) == {"prepare_threshold": None}
    asyncpg = normalize_url(TRANSACTION_POOLER.replace("postgresql://", "postgresql+asyncpg://"))
    assert connect_args(asyncpg, auto) == {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
    }
    assert connect_args(normalize_url("postgresql://u:p@localhost/db"), auto) == {}
    forced_off = Settings(database_prepared_statements=False)
    assert connect_args(normalize_url("postgresql://u:p@localhost/db"), forced_off) == {
        "prepare_threshold": None
    }


async def test_read_only_engine_refuses_writes(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_READ_ONLY", "true")
    monkeypatch.setenv("DATABASE_NULL_POOL", "true")
    get_settings.cache_clear()
    try:
        engine = create_engine(migrated_db)
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT count(*) FROM sources"))).scalar() == 0
        async with engine.connect() as conn:
            with pytest.raises(DBAPIError, match="read-only transaction"):
                await conn.execute(
                    text(
                        "INSERT INTO sources (platform, board_identifier, base_url)"
                        " VALUES ('x', 'y', 'z')"
                    )
                )
        await engine.dispose()
    finally:
        get_settings.cache_clear()
