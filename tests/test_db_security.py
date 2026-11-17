"""Database access lockdown (migration 0007): RLS everywhere, nothing for anon/authenticated.

The test database gets Supabase-like roles and default grants before the migrations run
(tests/conftest.py, SUPABASE_LIKE_ROLES_SQL), so this checks the hosted setup.
"""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from jobindex.dbsecurity import audit


async def test_audit_passes_after_migrations(session: AsyncSession) -> None:
    assert await audit(session) == []


async def test_audit_reports_rls_off_and_api_grants(session: AsyncSession) -> None:
    await session.execute(text("CREATE TABLE public.leaky (id int)"))
    await session.execute(text("GRANT SELECT ON public.leaky TO anon"))
    await session.execute(text("CREATE TABLE public.no_policy (id int)"))  # web gets SELECT
    await session.execute(text("ALTER TABLE public.no_policy ENABLE ROW LEVEL SECURITY"))
    await session.execute(text("GRANT UPDATE ON public.jobs TO jobindex_web"))
    try:
        problems = await audit(session)
    finally:
        await session.rollback()
    assert "table leaky: row level security is disabled" in problems
    assert "leaky: anon has SELECT" in problems
    assert "table no_policy: jobindex_web has SELECT but no SELECT policy" in problems
    assert "jobs: jobindex_web has UPDATE (only SELECT allowed)" in problems


def test_roles_see_what_they_should(migrated_db: str) -> None:
    engine = create_engine(migrated_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO sources (platform, board_identifier, base_url)"
                    " VALUES ('x', 'y', 'https://example.com')"
                )
            )
        with engine.connect() as conn:
            conn.execute(text("SET ROLE jobindex_web"))
            assert conn.scalar(text("SELECT count(*) FROM sources")) == 1
            conn.execute(
                text(
                    "INSERT INTO pending_sources (url, submitted_at)"
                    " VALUES ('https://example.com/careers', now())"
                )
            )
            with pytest.raises(ProgrammingError, match="permission denied"):
                conn.execute(text("DELETE FROM sources"))
            conn.rollback()
            for role in ("anon", "authenticated"):
                conn.execute(text(f"SET ROLE {role}"))
                for table in ("jobs", "sources", "pending_sources", "alembic_version"):
                    with pytest.raises(ProgrammingError, match="permission denied"):
                        conn.execute(text(f"SELECT 1 FROM {table}"))
                    conn.rollback()
                    conn.execute(text(f"SET ROLE {role}"))
    finally:
        engine.dispose()
