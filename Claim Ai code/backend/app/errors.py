"""Error-handling foundation.

FastAPI's normal handling of HTTPException (e.g. 404, 405) and request validation errors (422) is left unchanged,
so every error response uses FastAPI's ``{"detail": ...}`` shape.

An RBAC denial raised by code running in a request (``app.rbac.require_permission`` raising AuthorizationError) is
an expected 403, not an unexpected error. It gets the same generic 403 response as the ``permission_required``
dependency. The denial was already logged by app.rbac, so no traceback is logged.

Unexpected exceptions return a generic 500 with the same shape, so exception messages, file paths and stack traces
never reach the client. Exceptions raised while handling a request are logged by the request-logging middleware
(app/request_logging.py). The handler below is the fallback for anything raised outside that middleware.
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.rbac import FORBIDDEN_MESSAGE, AuthorizationError

INTERNAL_ERROR_DETAIL = "Internal server error"

logger = logging.getLogger(__name__)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette re-raises after this response, so on this rare path uvicorn logs the traceback as well.
    logger.error(
        "unhandled exception outside request handling method=%s path=%r",
        request.method,
        request.url.path,
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR_DETAIL})


async def authorization_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": FORBIDDEN_MESSAGE})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthorizationError, authorization_error_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
