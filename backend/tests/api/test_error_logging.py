"""API tests for error handling and request logging: safe client responses, server-side logs without secrets."""

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.config import Settings
from app.errors import INTERNAL_ERROR_DETAIL
from app.main import create_app

SECRET = "pw-x9"


def _app_with_password(settings: Settings) -> FastAPI:
    return create_app(settings.model_copy(update={"postgres_password": SecretStr(SECRET)}))


@pytest.mark.security
def test_unexpected_exception_is_logged_once_and_client_gets_generic_500(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    app = _app_with_password(settings)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"database connect failed: password={SECRET}")

    # Default TestClient re-raises server exceptions, so this also proves the exception does not escape the app.
    with TestClient(app) as client:
        response = client.get("/boom")
    logs = capsys.readouterr().err

    assert response.status_code == 500
    assert response.json() == {"detail": INTERNAL_ERROR_DETAIL}
    for leak in ("Traceback", "RuntimeError", ".py", SECRET):
        assert leak not in response.text
    assert logs.count("Traceback") == 1
    assert "ERROR app.request_logging: request failed method=GET path='/boom' status=500" in logs
    assert "RuntimeError: database connect failed: password=[REDACTED]" in logs
    assert SECRET not in logs


@pytest.mark.security
def test_exception_outside_request_logging_still_returns_generic_500_and_is_logged(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    app = _app_with_password(settings)

    async def broken(request: Request, call_next: RequestResponseEndpoint) -> Response:
        raise RuntimeError(f"middleware failure {SECRET}")

    app.add_middleware(BaseHTTPMiddleware, dispatch=broken)  # outermost: runs before the request-logging middleware

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/health")
    logs = capsys.readouterr().err

    assert response.status_code == 500
    assert response.json() == {"detail": INTERNAL_ERROR_DETAIL}
    assert SECRET not in response.text
    assert "ERROR app.errors: unhandled exception outside request handling method=GET path='/health'" in logs
    assert "RuntimeError: middleware failure [REDACTED]" in logs
    assert SECRET not in logs


@pytest.mark.parametrize(
    ("method", "path", "status", "detail"),
    [("GET", "/does-not-exist", 404, "Not Found"), ("POST", "/", 405, "Method Not Allowed")],
    ids=["404-unknown-route", "405-wrong-method"],
)
def test_client_errors_keep_fastapi_responses_and_are_logged_without_traceback(
    client: TestClient, capsys: pytest.CaptureFixture[str], method: str, path: str, status: int, detail: str
) -> None:
    response = client.request(method, path)
    logs = capsys.readouterr().err

    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert f"INFO app.request_logging: request method={method} path={path!r} status={status} duration_ms=" in logs
    assert "Traceback" not in logs


@pytest.mark.security
def test_request_log_contains_metadata_only(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    app = _app_with_password(settings)

    with TestClient(app) as client:
        client.get(
            "/?token=QUERY-TOKEN-1",
            headers={"Authorization": "Bearer AUTH-TOKEN-2", "Cookie": "session=COOKIE-3", "X-API-Key": "APIKEY-4"},
        )
        client.post("/", content=b'{"password": "BODY-SECRET-5"}', headers={"Content-Type": "application/json"})
        client.get("/forged%0Afake-entry%1B[2J")
    logs = capsys.readouterr().err

    assert "application configured" in logs  # the startup line was captured too, so the checks below cover it
    assert "request method=GET path='/' status=200 duration_ms=" in logs
    assert "request method=POST path='/' status=405 duration_ms=" in logs
    for value in ("QUERY-TOKEN-1", "token=", "AUTH-TOKEN-2", "Bearer", "COOKIE-3", "APIKEY-4", "BODY-SECRET-5", SECRET):
        assert value not in logs
    # A crafted path cannot forge a log line or inject terminal escapes: URL parsing drops the newline (stdlib
    # urlsplit removes \t \r \n), and repr() escapes any other control character, such as ESC.
    assert "path='/forgedfake-entry\\x1b[2J' status=404" in logs
    assert "\x1b" not in logs
    assert not any(line.startswith("fake-entry") for line in logs.splitlines())


def test_successful_health_checks_are_logged_only_at_debug(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
    at_info = capsys.readouterr().err

    with TestClient(create_app(settings.model_copy(update={"log_level": "DEBUG"}))) as client:
        assert client.get("/health").json() == {"status": "healthy"}
    at_debug = capsys.readouterr().err

    assert "path='/health'" not in at_info
    assert "DEBUG app.request_logging: request method=GET path='/health' status=200" in at_debug
