"""Service-level errors (D-035), without a database: an unreachable or misconfigured database is a generic 503 with
the reason in the server log (it was an unexplained 500), the startup log names a missing database password, and a
request body over ``MAX_UPLOAD_BYTES`` is a 413 before any route, authentication included, runs."""

import logging
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import app.api.database as database_module
from app.api.database import DATABASE_UNAVAILABLE
from app.config import Settings
from app.main import create_app
from app.request_limits import REQUEST_TOO_LARGE


def _refuse(*args: Any, **kwargs: Any) -> None:
    # What libpq reports when the backend runs without POSTGRES_PASSWORD (the observed sign-in 500).
    raise psycopg.OperationalError('connection failed: connection to server at "127.0.0.1", port 5432 failed: '
                                   "fe_sendauth: no password supplied")


@pytest.mark.parametrize(("path", "body"),
                         [("/auth/login", {"company_slug": "acme", "email": "a@b.example", "password": "pw-123"}),
                          ("/auth/register-company", {"company_name": "Acme", "company_slug": "acme",
                                                      "admin_full_name": "A", "admin_email": "a@b.example",
                                                      "admin_password": "a long enough password"})],
                         ids=["sign-in", "registration"])
def test_an_unreachable_database_is_a_503_with_the_reason_in_the_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, path: str, body: dict[str, str]
) -> None:
    monkeypatch.setattr(database_module, "connect", _refuse)

    with caplog.at_level(logging.ERROR, logger="app"), TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.post(path, json=body)

    assert (response.status_code, response.json()) == (503, {"detail": DATABASE_UNAVAILABLE})
    assert "database connection failed error=OperationalError" in caplog.text
    assert "fe_sendauth: no password supplied" in caplog.text  # the operator can see why
    assert "pw-123" not in caplog.text and "a long enough password" not in caplog.text
    assert "fe_sendauth" not in response.text and "5432" not in response.text


@pytest.mark.parametrize(("password", "warned"), [(None, True), ("set-locally", False)], ids=["missing", "set"])
def test_startup_warns_when_the_database_password_is_missing(
    caplog: pytest.LogCaptureFixture, password: str | None, warned: bool
) -> None:
    with caplog.at_level(logging.WARNING, logger="app"):
        create_app(Settings(_env_file=None, postgres_password=password))

    assert ("POSTGRES_PASSWORD is not set" in caplog.text) is warned
    assert "set-locally" not in caplog.text


@pytest.mark.security
def test_a_body_over_the_limit_is_a_413_before_authentication_or_any_route(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = create_app(Settings(_env_file=None, max_upload_bytes=1024))

    with caplog.at_level(logging.WARNING, logger="app"), TestClient(app) as client:
        too_large = client.post("/chat", content=b"x" * 1025, headers={"Content-Type": "application/pdf"})
        at_limit = client.post("/chat", content=b"{" + b" " * 1022 + b"}", headers={"Content-Type": "application/json"})
        no_body = client.get("/health")

    assert (too_large.status_code, too_large.json()) == (413, {"detail": REQUEST_TOO_LARGE})
    assert at_limit.status_code == 401  # within the limit: reaches the route, which needs a token
    assert no_body.status_code == 200
    assert "request refused: body over the limit method=POST path='/chat' max_bytes=1024" in caplog.text
