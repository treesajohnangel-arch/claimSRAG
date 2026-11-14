"""ClaimsRAG FastAPI application.

Run from the backend/ directory:
    .venv/bin/uvicorn app.main:app --reload
"""

import logging

from fastapi import FastAPI
from pydantic import BaseModel

from app.api.auth import router as auth_router
from app.api.authorization import routes_missing_permission
from app.api.chat import router as chat_router
from app.api.health import router as health_router
from app.api.policies import router as policies_router
from app.api.search import router as search_router
from app.config import Settings
from app.errors import register_error_handlers
from app.logging_config import configure_logging
from app.request_limits import RequestSizeLimit
from app.request_logging import register_request_logging

logger = logging.getLogger(__name__)


class ServiceInfo(BaseModel):
    name: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)

    app = FastAPI(title=settings.app_name, version=settings.app_version)
    # Read by request dependencies that need configuration, e.g. app.api.authorization for the JWT public key.
    app.state.settings = settings
    register_error_handlers(app)
    register_request_logging(app)
    app.add_middleware(RequestSizeLimit, max_bytes=settings.max_upload_bytes)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(policies_router)
    app.include_router(search_router)
    app.include_router(chat_router)

    @app.get("/", tags=["service"])
    def service_info() -> ServiceInfo:
        return ServiceInfo(name=settings.app_name, version=settings.app_version)

    # Fail closed: every route registered above, except the public ones, must declare its permission.
    missing = routes_missing_permission(app)
    if missing:
        raise RuntimeError(f"Routes without a declared permission (see app/api/authorization.py): {missing}")

    if settings.postgres_password is None:
        # The most common local misconfiguration: every database request would fail (503). See backend/.env.example.
        logger.warning("POSTGRES_PASSWORD is not set: database requests will fail until it is configured")
    logger.info(
        "application configured name=%s version=%s environment=%s log_level=%s",
        settings.app_name,
        settings.app_version,
        settings.environment,
        settings.log_level,
    )
    return app


app = create_app()
