"""Request logging and the boundary for unexpected exceptions.

Each request is logged once, with metadata only: method, path (without the query string), status and duration.
Headers (Authorization, Cookie, ...), query strings and bodies are never logged. The path is written with repr(), so
a decoded newline in it cannot start a forged log line.

An exception raised while handling a request is logged here once, with its traceback, and the client gets the
generic 500 response. Because it is handled here, it never reaches Starlette's ServerErrorMiddleware, which would
re-raise it to the server, so uvicorn does not log the same traceback a second time.
"""

import logging
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.errors import INTERNAL_ERROR_DETAIL

logger = logging.getLogger(__name__)

# Docker's health check calls /health every 10 s. Successful calls are logged at DEBUG to keep INFO logs readable.
_QUIET_PATHS = frozenset({"/health"})


async def log_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request failed method=%s path=%r status=500 duration_ms=%.1f",
            request.method,
            request.url.path,
            _elapsed_ms(started),
        )
        return JSONResponse(status_code=500, content={"detail": INTERNAL_ERROR_DETAIL})

    quiet = request.url.path in _QUIET_PATHS and response.status_code < 400
    logger.log(
        logging.DEBUG if quiet else logging.INFO,
        "request method=%s path=%r status=%d duration_ms=%.1f",
        request.method,
        request.url.path,
        response.status_code,
        _elapsed_ms(started),
    )
    return response


def register_request_logging(app: FastAPI) -> None:
    app.add_middleware(BaseHTTPMiddleware, dispatch=log_requests)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
