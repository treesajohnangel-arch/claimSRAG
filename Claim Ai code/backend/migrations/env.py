"""Alembic environment for ClaimsRAG.

Migrations are written as SQL (``op.execute``). There are no ORM models, so ``target_metadata`` is None and
autogenerate is not used. The database URL comes from Settings, unless a caller (the database tests) passes one in
``config.attributes["sqlalchemy_url"]``.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL

from app.config import Settings
from app.db import database_url

config = context.config

# The command-line tool gets the logging set up in alembic.ini. Callers inside another process (pytest) skip it,
# so the application's own logging configuration is left untouched.
if config.config_file_name is not None and not config.attributes.get("skip_logging_config"):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _url() -> URL:
    return config.attributes.get("sqlalchemy_url") or database_url(Settings())


def run_migrations_offline() -> None:
    """Emit the migration SQL without connecting (``alembic upgrade head --sql``)."""
    context.configure(url=_url(), target_metadata=None, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
