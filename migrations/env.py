"""Alembic environment. Runs migrations synchronously with psycopg 3."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from jobindex.config import get_settings
from jobindex.db import create_sync_engine_args
from jobindex.models import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    # Tests pass an explicit URL via config attributes; otherwise use DATABASE_URL.
    url = config.attributes.get("database_url")
    return str(url) if url else get_settings().database_url


def run_migrations_offline() -> None:
    url, _ = create_sync_engine_args(_database_url())
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Works through the Supabase session pooler too (prepared statements off there).
    url, connect_args = create_sync_engine_args(_database_url())
    engine = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
