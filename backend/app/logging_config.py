"""Application logging: standard-library logging, configured once by create_app, written to stderr.

Modules log with ``logger = logging.getLogger(__name__)`` and never configure logging themselves.
Never log secrets, credentials, Authorization/Cookie headers, tokens, query strings, request bodies, or
document/policy text.
"""

import logging
import sys
from typing import TextIO

from app.config import Settings

APP_LOGGER = "app"  # parent of every module logger in this package (app.main, app.errors, ...)
LOG_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"
REDACTED = "[REDACTED]"
HANDLER_NAME = "claimsrag-stderr"


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to the current ``sys.stderr`` at emit time, not the stream that existed when it was created."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)  # skip StreamHandler.__init__, which would pin the stream

    @property
    def stream(self) -> TextIO:  # type: ignore[override]
        return sys.stderr


class _RedactingFormatter(logging.Formatter):
    """Formats a record, then replaces configured secret values anywhere in it, including exception text."""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__(LOG_FORMAT, DATE_FORMAT)
        self._secrets = secrets

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(settings: Settings) -> None:
    """Configure the "app" logger: level, a single stderr handler, and secret redaction. Safe to call again."""
    logger = logging.getLogger(APP_LOGGER)
    for old in [h for h in logger.handlers if h.get_name() == HANDLER_NAME]:
        logger.removeHandler(old)

    # Every secret setting must be listed here.
    secrets = tuple(
        s.get_secret_value() for s in (settings.postgres_password, settings.jwt_private_key) if s and s.get_secret_value()
    )
    handler = _StderrHandler()
    handler.set_name(HANDLER_NAME)
    handler.setFormatter(_RedactingFormatter(secrets))
    logger.addHandler(handler)
    logger.setLevel(settings.log_level)
    # Keep app records off the root logger, so a library that configures root cannot print them twice.
    logger.propagate = False
