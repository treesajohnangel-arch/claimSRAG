"""The default suite must not reach the network (PostgreSQL, Docker services, live APIs)."""

import socket

import psycopg
import pytest
from sqlalchemy import create_engine


def test_tcp_connections_are_refused_outside_database_tests() -> None:
    # Arrange: the target is the local PostgreSQL port, so a missing guard would show up as a real connection
    # attempt, not as an error from this test.
    address = ("127.0.0.1", 5432)

    # Act / Assert
    with pytest.raises(RuntimeError, match="Network access in a non-database test"):
        socket.create_connection(address, timeout=1)


def test_database_connections_are_refused_outside_database_tests() -> None:
    # psycopg connects through libpq (C), which the socket guard cannot see, so it is guarded separately. Every
    # route in is covered: psycopg.connect, Connection.connect, and a SQLAlchemy engine using the psycopg driver.
    target = {"host": "127.0.0.1", "port": 5432, "dbname": "postgres", "connect_timeout": 1}
    engine = create_engine("postgresql+psycopg://nobody@127.0.0.1:5432/postgres")

    with pytest.raises(RuntimeError, match="Database connection in a non-database test"):
        psycopg.connect(**target)
    with pytest.raises(RuntimeError, match="Database connection in a non-database test"):
        psycopg.Connection.connect(**target)
    with pytest.raises(RuntimeError, match="Database connection in a non-database test"):
        engine.connect()
    engine.dispose()
