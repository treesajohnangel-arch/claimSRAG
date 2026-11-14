"""Shared test configuration and fixtures.

Layers: tests/unit (no application or HTTP), tests/api (the app in-process through TestClient), and tests/database
(marked `database`; need PostgreSQL; deselected by default). See "Testing rules" in CLAUDE.md.
"""

import base64
import json
import logging
import os
import socket
from collections.abc import Iterator
from typing import Any, NamedTuple, NoReturn

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

SETTINGS_ENV_VARS = (
    "APP_NAME",
    "APP_VERSION",
    "ENVIRONMENT",
    "DEBUG",
    "LOG_LEVEL",
    "HOST",
    "PORT",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "JWT_PRIVATE_KEY",
    "JWT_PUBLIC_KEY",
    "ACCESS_TOKEN_LIFETIME_MINUTES",
    "OLLAMA_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_OUTPUT_TOKENS",
    "DOCUMENT_STORAGE_DIR",
    "MAX_UPLOAD_BYTES",
    "DEMO_COMPANY_SLUG",
    "DEMO_USER_EMAIL",
)

# Database tests (tests/database) need the PostgreSQL server's address and credentials from the shell. Keep them
# here, before the isolation below removes them; only the `database_env` fixture hands them out.
_DATABASE_ENV = {name: os.environ.get(name) for name in SETTINGS_ENV_VARS if name.startswith("POSTGRES_")}

# Session-wide isolation. This runs before any app module is imported, because app.main builds a module-level app.
# It drops ClaimsRAG settings inherited from the developer's shell, and selects the test environment, in which
# backend/.env is not read.
for _name in SETTINGS_ENV_VARS:
    os.environ.pop(_name, None)
os.environ["ENVIRONMENT"] = "test"

from app.config import Settings  # noqa: E402
from app.logging_config import APP_LOGGER, HANDLER_NAME  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _block_network_outside_database_tests(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast when a test that is not marked `database` opens a TCP or database connection.

    This keeps the default suite independent of PostgreSQL, Docker and any running service. A test marked `llm` may
    connect to loopback addresses only (the local Ollama server): any other address still fails, which also proves
    answering never leaves the machine.
    """
    if request.node.get_closest_marker("database"):
        return
    loopback_allowed = request.node.get_closest_marker("llm") is not None
    real_connect = socket.socket.connect

    def refuse_database_connection(*args: Any, **kwargs: Any) -> NoReturn:
        raise RuntimeError("Database connection in a non-database test. Mark the test 'database'.")

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            if loopback_allowed and address[0] in ("127.0.0.1", "::1"):
                real_connect(sock, address)
                return
            sock.close()  # callers such as socket.create_connection only close on OSError
            raise RuntimeError(
                f"Network access in a non-database test ({address!r}). Mock it, or mark the test 'database'."
            )
        real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    # psycopg connects through libpq, in C, below the Python socket layer, so the patch above cannot see it.
    monkeypatch.setattr(psycopg, "connect", refuse_database_connection)
    monkeypatch.setattr(psycopg.Connection, "connect", classmethod(refuse_database_connection))
    monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(refuse_database_connection))


@pytest.fixture(scope="session")
def database_env() -> dict[str, str | None]:
    """POSTGRES_* values from the shell that started pytest. Only tests/database uses them."""
    return dict(_DATABASE_ENV)


@pytest.fixture(autouse=True)
def _restore_app_logger() -> Iterator[None]:
    """Logging configuration is process-wide: undo any change a test makes to the "app" logger.

    Only ClaimsRAG's own handler, the level and propagation are restored. pytest attaches and removes its own capture
    handlers for each test phase, and those must not be saved and re-added here.
    """
    logger = logging.getLogger(APP_LOGGER)

    def ours() -> list[logging.Handler]:
        return [h for h in logger.handlers if h.get_name() == HANDLER_NAME]

    level, propagate, saved = logger.level, logger.propagate, ours()
    yield
    for handler in ours():
        logger.removeHandler(handler)
    for handler in saved:
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Default settings, isolated from the developer's shell environment and backend/.env."""
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    """A fresh application built from the isolated default settings."""
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


class RsaKeyPair(NamedTuple):
    private_pem: str
    public_pem: str


def generate_rsa_key_pair(bits: int = 2048) -> RsaKeyPair:
    """A throwaway RSA key pair, in the PEM formats `openssl genpkey` / `openssl pkey -pubout` produce. Keys exist
    only in memory for the test run; none is ever written to the repository."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    return RsaKeyPair(private_pem, public_pem)


@pytest.fixture(scope="session")
def jwt_keys() -> RsaKeyPair:
    """The test session's JWT signing key pair."""
    return generate_rsa_key_pair()


def forge_claims(token: str, **changes: object) -> str:
    """A forgery: the token with some payload claims changed and the original signature kept."""
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))) | changes
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"{header}.{forged}.{signature}"


def make_pdf(*pages: str | tuple[str, ...] | None) -> bytes:
    """A PDF built in memory, one page per argument: None is a blank page, a string one text block, and a tuple
    several blocks placed well apart (separate paragraphs). Nothing is written to disk."""
    import pymupdf

    with pymupdf.open() as pdf:
        for content in pages:
            page = pdf.new_page()
            blocks = (content,) if isinstance(content, str) else content or ()
            for index, block in enumerate(blocks):
                page.insert_text((72, 72 + 160 * index), block)
        return pdf.tobytes()


def make_scanned_pdf(page_count: int = 2) -> bytes:
    """A PDF whose pages hold only an image and no text layer, like a scan."""
    import pymupdf

    with pymupdf.open() as pdf:
        for _ in range(page_count):
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 20, 20), False)
            pixmap.clear_with(200)
            pdf.new_page().insert_image(pymupdf.Rect(72, 72, 300, 300), pixmap=pixmap)
        return pdf.tobytes()
