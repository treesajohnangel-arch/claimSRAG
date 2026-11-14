"""Unit tests for logging configuration: level validation, level applied, line format, secret redaction."""

import logging
import re

import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings
from app.logging_config import REDACTED, configure_logging
from tests.conftest import RsaKeyPair

SECRET = "pw-x9"
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) [\w.]+: ")


def test_log_level_defaults_to_info(settings: Settings) -> None:
    assert settings.log_level == "INFO"


@pytest.mark.parametrize("value", ["debug", "INFO", "Warning", "ERROR", "critical"])
def test_known_log_levels_are_accepted_in_any_case(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("LOG_LEVEL", value)

    settings = Settings(_env_file=None)

    assert settings.log_level == value.upper()


def test_unknown_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")

    with pytest.raises(ValidationError, match="'DEBUG', 'INFO', 'WARNING', 'ERROR' or 'CRITICAL'"):
        Settings(_env_file=None)


def test_configured_level_is_applied(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    log = logging.getLogger("app.unit_test")

    configure_logging(settings.model_copy(update={"log_level": "WARNING"}))
    log.info("info-at-warning")
    log.warning("warning-at-warning")
    at_warning = capsys.readouterr().err

    configure_logging(settings.model_copy(update={"log_level": "DEBUG"}))
    log.debug("debug-at-debug")
    at_debug = capsys.readouterr().err

    assert "warning-at-warning" in at_warning
    assert "info-at-warning" not in at_warning
    assert "debug-at-debug" in at_debug


def test_log_line_has_timestamp_level_logger_and_message_once(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    # Configuring twice (e.g. create_app called again) must not add a second handler.
    configure_logging(settings)
    configure_logging(settings)

    logging.getLogger("app.unit_test").info("hello %s", "world")
    lines = capsys.readouterr().err.splitlines()

    assert len(lines) == 1
    assert LOG_LINE.match(lines[0])
    assert lines[0].endswith(" INFO app.unit_test: hello world")


@pytest.mark.security
def test_configured_secret_is_redacted_from_messages_and_tracebacks(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings.model_copy(update={"postgres_password": SecretStr(SECRET)}))
    log = logging.getLogger("app.unit_test")

    try:
        raise RuntimeError(f"connection failed for password {SECRET}")
    except RuntimeError:
        log.exception("could not connect using %s", SECRET)
    output = capsys.readouterr().err

    assert "Traceback" in output
    assert f"could not connect using {REDACTED}" in output
    assert f"RuntimeError: connection failed for password {REDACTED}" in output
    assert SECRET not in output


@pytest.mark.security
def test_jwt_private_key_is_redacted_from_logs(
    settings: Settings, jwt_keys: RsaKeyPair, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings.model_copy(update={"jwt_private_key": SecretStr(jwt_keys.private_pem)}))

    logging.getLogger("app.unit_test").error("loaded signing key %s", jwt_keys.private_pem)
    output = capsys.readouterr().err

    assert f"loaded signing key {REDACTED}" in output
    for line in jwt_keys.private_pem.splitlines():
        if not line.startswith("-----"):
            assert line not in output
