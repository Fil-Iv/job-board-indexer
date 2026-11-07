"""Guards against drift between the ORM models and the Alembic migrations."""

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, text

from jobindex.models import Base
from tests.conftest import alembic_config

CHECK_SCHEMA = "metadata_check"

# Constraint and index definitions, comparable across schemas.
DEFINITIONS_SQL = """
SELECT c.relname || ':' || con.conname || ':' || pg_get_constraintdef(con.oid)
FROM pg_constraint con
JOIN pg_class c ON c.oid = con.conrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :schema
UNION ALL
SELECT tablename || ':' || indexname || ':' || replace(indexdef, :schema || '.', '')
FROM pg_indexes WHERE schemaname = :schema
UNION ALL
SELECT table_name || ':' || column_name || ':' || coalesce(generation_expression, '')
FROM information_schema.columns
WHERE table_schema = :schema AND is_generated = 'ALWAYS'
"""


def _definitions(conn: Connection, schema: str) -> set[str]:
    rows = conn.execute(text(DEFINITIONS_SQL), {"schema": schema}).scalars()
    return {row.replace(f"{schema}.", "") for row in rows}


def test_models_match_migrations(migrated_db: str) -> None:
    engine = create_engine(migrated_db)
    try:
        with engine.connect() as conn:
            # Columns, types, nullability, FKs, indexes (Alembic's autogenerate diff).
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
            assert diff == [], f"models and migrations differ: {diff}"

        # Constraint names, CHECK constraints and generated-column expressions, which
        # autogenerate does not compare: build the schema from the models in a scratch
        # schema and compare with the migrated one.
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA IF EXISTS {CHECK_SCHEMA} CASCADE"))
            conn.execute(text(f"CREATE SCHEMA {CHECK_SCHEMA}"))
            Base.metadata.create_all(
                conn.execution_options(schema_translate_map={None: CHECK_SCHEMA})
            )
            from_models = _definitions(conn, CHECK_SCHEMA)
            from_migrations = _definitions(conn, "public") - {
                d for d in _definitions(conn, "public") if d.startswith("alembic_version:")
            }
            conn.execute(text(f"DROP SCHEMA {CHECK_SCHEMA} CASCADE"))
        assert from_models == from_migrations
    finally:
        engine.dispose()


def test_downgrade_and_upgrade_roundtrip(migrated_db: str) -> None:
    cfg = alembic_config(migrated_db)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
