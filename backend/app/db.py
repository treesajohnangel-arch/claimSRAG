"""Database connection settings and connections.

``database_url`` builds the SQLAlchemy URL (driver: psycopg 3) that Alembic uses, from ``Settings``.
``str()`` and ``repr()`` of the URL hide the password.

``connect`` opens one psycopg connection for one API request (Phase 2.4; see app/api/database.py). There is no pool
yet. The connection is in autocommit mode, so nothing is written outside an explicit ``conn.transaction()`` block.
"""

from typing import Any

import psycopg
from sqlalchemy.engine import URL

from app.config import Settings

CONNECT_TIMEOUT_SECONDS = 10


def database_url(settings: Settings) -> URL:
    password = settings.postgres_password.get_secret_value() if settings.postgres_password else None
    return URL.create(
        "postgresql+psycopg",
        username=settings.postgres_user,
        password=password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_db,
    )


def connect(settings: Settings) -> psycopg.Connection[Any]:
    """A new connection to the configured database, in autocommit mode."""
    password = settings.postgres_password.get_secret_value() if settings.postgres_password else None
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=password,
        dbname=settings.postgres_db,
        autocommit=True,
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
    )
