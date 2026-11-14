"""Fixtures for tests that need PostgreSQL. Every module here is marked `database`; run with `pytest -m database`.

Safety:
- Each test session creates its own database, ``claimsrag_test_<random>``, migrates it with Alembic and drops it at
  the end. Nothing else is ever dropped. The development database (``claimsrag``) is never connected to.
- Only a local server is accepted.
- The server address and credentials come from the POSTGRES_HOST / POSTGRES_PORT / POSTGRES_USER /
  POSTGRES_PASSWORD variables of the shell that started pytest. POSTGRES_DB is ignored. If they are missing, the
  tests fail; they are never skipped.
- Each test gets a connection whose changes are rolled back afterwards.
"""

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import sql
from psycopg.rows import DictRow, dict_row

from app.config import Settings
from app.db import database_url

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "postgres"})
TEST_DATABASE_PREFIX = "claimsrag_test_"
# A fake value in PHC format. It is not the hash of any password.
PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZS1oYXNoLWZvci10ZXN0cw"

Row = dict[str, Any]


@pytest.fixture(scope="session")
def server_settings(database_env: dict[str, str | None]) -> Settings:
    missing = [name for name in ("POSTGRES_USER", "POSTGRES_PASSWORD") if not database_env.get(name)]
    if missing:
        pytest.fail(
            f"Database tests need {', '.join(missing)} in the environment (see 'Testing rules' in CLAUDE.md).",
            pytrace=False,
        )
    values = {name.lower(): value for name, value in database_env.items() if value and name != "POSTGRES_DB"}
    settings = Settings(_env_file=None, **values)
    if settings.postgres_host not in LOCAL_HOSTS:
        pytest.fail(
            f"Database tests only run against a local PostgreSQL, not POSTGRES_HOST={settings.postgres_host!r}.",
            pytrace=False,
        )
    return settings


def _connect(settings: Settings, dbname: str) -> psycopg.Connection[DictRow]:
    password = settings.postgres_password.get_secret_value() if settings.postgres_password else None
    return psycopg.connect(
        host=settings.postgres_host,
        port=settings.postgres_port,
        user=settings.postgres_user,
        password=password,
        dbname=dbname,
        autocommit=True,
        connect_timeout=5,
        row_factory=dict_row,
    )


@contextmanager
def _temporary_database(server: Settings) -> Iterator[Settings]:
    name = TEST_DATABASE_PREFIX + secrets.token_hex(6)
    with _connect(server, "postgres") as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield server.model_copy(update={"postgres_db": name})
    finally:
        with _connect(server, "postgres") as admin:
            # Drops only the database created above.
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def run_alembic(settings: Settings, action: str, revision: str) -> None:
    """Run ``alembic upgrade|downgrade <revision>`` against the database in ``settings``."""
    config = Config(str(ALEMBIC_INI))
    config.attributes["sqlalchemy_url"] = database_url(settings)
    config.attributes["skip_logging_config"] = True
    getattr(command, action)(config, revision)


@pytest.fixture
def empty_database(server_settings: Settings) -> Iterator[Settings]:
    """A brand-new, empty database (no migrations applied), dropped after the test."""
    with _temporary_database(server_settings) as settings:
        yield settings


@pytest.fixture(scope="session")
def migrated_database(server_settings: Settings) -> Iterator[Settings]:
    """One database per session, migrated to head from empty."""
    with _temporary_database(server_settings) as settings:
        run_alembic(settings, "upgrade", "head")
        yield settings


@pytest.fixture
def connect() -> Callable[[Settings], psycopg.Connection[DictRow]]:
    return lambda settings: _connect(settings, settings.postgres_db)


@pytest.fixture
def db(migrated_database: Settings) -> Iterator[psycopg.Connection[DictRow]]:
    """A connection to the migrated database. Everything the test does is rolled back.

    Wrap a statement that is expected to fail in ``with db.transaction():``, which is a savepoint, so the test can
    continue after the error.
    """
    with _connect(migrated_database, migrated_database.postgres_db) as conn:
        with conn.transaction(force_rollback=True):
            yield conn


def _insert(conn: psycopg.Connection[DictRow], table: str, values: Row) -> Row:
    query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(table),
        sql.SQL(", ").join(map(sql.Identifier, values)),
        sql.SQL(", ").join(sql.Placeholder() * len(values)),
    )
    row = conn.execute(query, list(values.values())).fetchone()
    assert row is not None
    return row


@pytest.fixture
def insert_company(db: psycopg.Connection[DictRow]) -> Callable[..., Row]:
    """Insert a company. Columns not given use valid values; a column given as None is inserted as NULL."""

    def insert(**values: Any) -> Row:
        return _insert(db, "companies", {"name": "Acme Insurance", "slug": f"acme-{secrets.token_hex(4)}", **values})

    return insert


@pytest.fixture
def insert_user(db: psycopg.Connection[DictRow], insert_company: Callable[..., Row]) -> Callable[..., Row]:
    """Insert a user (in a new company unless company_id is given). Same conventions as insert_company."""

    def insert(**values: Any) -> Row:
        if "company_id" not in values:
            values["company_id"] = insert_company()["id"]
        defaults = {
            "email": f"user-{secrets.token_hex(4)}@example.com",
            "password_hash": PASSWORD_HASH,
            "full_name": "Test User",
            "role": "STAFF",
        }
        return _insert(db, "users", {**defaults, **values})

    return insert


@pytest.fixture
def insert_policy(db: psycopg.Connection[DictRow], insert_company: Callable[..., Row]) -> Callable[..., Row]:
    """Insert a policy (in a new company unless company_id is given). Same conventions as insert_company."""

    def insert(**values: Any) -> Row:
        if "company_id" not in values:
            values["company_id"] = insert_company()["id"]
        defaults = {"policy_number": f"POL-{secrets.token_hex(4).upper()}", "name": "Family Health Cover",
                    "insurance_type": "HEALTH"}
        return _insert(db, "policies", {**defaults, **values})

    return insert


@pytest.fixture
def insert_policy_document(db: psycopg.Connection[DictRow], insert_policy: Callable[..., Row]) -> Callable[..., Row]:
    """Insert a policy document. Without policy_id, a new policy is created (in company_id's company, if given);
    without company_id, the policy's own company is used. Same conventions as insert_company."""

    def insert(**values: Any) -> Row:
        if "policy_id" not in values:
            policy = insert_policy(**({"company_id": values["company_id"]} if "company_id" in values else {}))
            values = {"policy_id": policy["id"], "company_id": policy["company_id"], **values}
        elif "company_id" not in values and values["policy_id"] is not None:
            owner = db.execute("SELECT company_id FROM policies WHERE id = %s", (values["policy_id"],)).fetchone()
            values["company_id"] = owner["company_id"] if owner else None
        token = secrets.token_hex(8)
        defaults = {
            "document_key": "policy-wording",
            "version": 1,
            "original_filename": "policy-wording.pdf",
            "storage_reference": f"test-documents/{token}.pdf",
            "content_type": "application/pdf",
            "size_bytes": 123_456,
            "checksum_sha256": secrets.token_hex(32),
        }
        return _insert(db, "policy_documents", {**defaults, **values})

    return insert
