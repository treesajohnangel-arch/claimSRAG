"""API tests: the application in-process through FastAPI's TestClient (no server, no network)."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.errors import INTERNAL_ERROR_DETAIL
from app.main import app as module_app
from app.main import create_app

# Deliberately short, so a leak cannot hide behind pydantic's truncation of long input values.
SECRET = "pw-x9"


def test_create_app_returns_configured_fastapi_app(settings: Settings) -> None:
    app = create_app(settings)

    assert isinstance(app, FastAPI)
    assert app.title == "ClaimsRAG"
    assert app.version == "0.1.0"


def test_module_level_app_is_importable() -> None:
    assert isinstance(module_app, FastAPI)


def test_service_info_reports_name_and_version(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.json() == {"name": "ClaimsRAG", "version": "0.1.0"}


def test_unknown_route_keeps_fastapi_404(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.security
@pytest.mark.parametrize("debug", [False, True])
def test_unhandled_error_returns_generic_500_without_internals(settings: Settings, debug: bool) -> None:
    app = create_app(settings.model_copy(update={"debug": debug}))

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {"detail": INTERNAL_ERROR_DETAIL}
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text


@pytest.mark.security
def test_secret_is_not_exposed_in_settings_output_or_api_metadata(settings: Settings) -> None:
    configured = settings.model_copy(update={"postgres_password": SecretStr(SECRET)})

    for text in (repr(configured), str(configured), configured.model_dump_json()):
        assert SECRET not in text

    with TestClient(create_app(configured)) as client:
        for path in ("/", "/health", "/openapi.json"):
            response = client.get(path)
            assert response.status_code == 200
            assert SECRET not in response.text
