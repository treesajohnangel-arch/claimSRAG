"""Unit tests for the database URL and the connection settings built from Settings (no database)."""

from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.db import CONNECT_TIMEOUT_SECONDS, connect, database_url


@pytest.mark.security
def test_database_url_is_built_from_settings_and_hides_the_password(settings: Settings) -> None:
    configured = settings.model_copy(
        update={
            "postgres_host": "db.local",
            "postgres_port": 6543,
            "postgres_db": "claimsrag",
            "postgres_user": "claimsrag",
            "postgres_password": SecretStr("p@ss:w/rd%"),
        }
    )

    url = database_url(configured)

    assert (url.drivername, url.host, url.port, url.database, url.username) == (
        "postgresql+psycopg",
        "db.local",
        6543,
        "claimsrag",
        "claimsrag",
    )
    assert url.password == "p@ss:w/rd%"
    for text in (str(url), repr(url)):
        assert "p@ss" not in text
    # Special characters are escaped when the URL is rendered for the driver.
    assert "p%40ss%3Aw%2Frd%25" in url.render_as_string(hide_password=False)


def test_database_url_without_a_password(settings: Settings) -> None:
    assert database_url(settings).password is None


@pytest.mark.security
def test_connect_opens_an_autocommit_connection_from_settings(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Autocommit: nothing an API route writes is committed except inside its explicit transaction block.
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: calls.append(kwargs) or "connection")
    configured = settings.model_copy(update={"postgres_host": "db.local", "postgres_port": 6543,
                                             "postgres_db": "claimsrag", "postgres_user": "claimsrag",
                                             "postgres_password": SecretStr("p@ss:w/rd%")})

    assert connect(configured) == "connection"
    assert calls == [{"host": "db.local", "port": 6543, "user": "claimsrag", "password": "p@ss:w/rd%",
                      "dbname": "claimsrag", "autocommit": True, "connect_timeout": CONNECT_TIMEOUT_SECONDS}]
