"""Unit tests for Settings: no application instance, no HTTP, no network."""

from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import ValidationError

from app.auth import AuthenticatedUser
from app.config import BACKEND_DIR, EXAMPLE_POSTGRES_PASSWORD, Settings
from app.models import UserRole, UserStatus
from app.tokens import decode_access_token, issue_access_token
from tests.conftest import RsaKeyPair, generate_rsa_key_pair

# Deliberately short, so a leak cannot hide behind pydantic's truncation of long input values.
SECRET = "pw-x9"


# --- Defaults and environment variables ---------------------------------------------------------------------


def test_settings_defaults(settings: Settings) -> None:
    assert settings.app_name == "ClaimsRAG"
    assert settings.app_version == "0.1.0"
    assert settings.environment == "development"
    assert settings.debug is False


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_NAME", "ClaimsRAG Test")
    monkeypatch.setenv("APP_VERSION", "9.9.9")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("DEBUG", "true")

    settings = Settings(_env_file=None)

    assert settings.app_name == "ClaimsRAG Test"
    assert settings.app_version == "9.9.9"
    assert settings.environment == "test"
    assert settings.debug is True


def test_database_settings_defaults(settings: Settings) -> None:
    assert settings.postgres_host == "localhost"
    assert settings.postgres_port == 5432
    assert settings.postgres_db == "claimsrag"
    assert settings.postgres_user == "claimsrag"
    assert settings.postgres_password is None


def test_database_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_HOST", "postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "claimsrag_test")
    monkeypatch.setenv("POSTGRES_USER", "claimsrag_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "not-a-real-secret")

    settings = Settings(_env_file=None)

    assert settings.postgres_host == "postgres"
    assert settings.postgres_port == 5433
    assert settings.postgres_db == "claimsrag_test"
    assert settings.postgres_user == "claimsrag_user"
    assert settings.postgres_password is not None
    assert settings.postgres_password.get_secret_value() == "not-a-real-secret"
    assert "not-a-real-secret" not in repr(settings)


# --- Test isolation -----------------------------------------------------------------------------------------


def test_test_session_runs_in_test_environment() -> None:
    # conftest selects ENVIRONMENT=test for the whole session, so a plain Settings() is the test configuration.
    settings = Settings()

    assert settings.environment == "test"
    assert settings.postgres_password is None


def test_test_environment_does_not_read_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("APP_NAME=FromDotEnv\nPOSTGRES_PASSWORD=from-dotenv\n", encoding="utf-8")
    monkeypatch.setitem(Settings.model_config, "env_file", dotenv)

    monkeypatch.setenv("ENVIRONMENT", "test")
    in_test = Settings()
    monkeypatch.setenv("ENVIRONMENT", "development")
    in_development = Settings()

    assert in_test.app_name == "ClaimsRAG"
    assert in_test.postgres_password is None
    # The same file is read outside the test environment, so the check above is meaningful.
    assert in_development.app_name == "FromDotEnv"


# --- Application and server settings ------------------------------------------------------------------------


def test_environment_must_be_a_known_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "prod")

    with pytest.raises(ValidationError, match="'development', 'test' or 'production'"):
        Settings(_env_file=None)


def test_server_settings_defaults_and_overrides(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (settings.host, settings.port) == ("127.0.0.1", 8000)

    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")
    overridden = Settings(_env_file=None)

    assert (overridden.host, overridden.port) == ("0.0.0.0", 9000)


@pytest.mark.parametrize("name", ["PORT", "POSTGRES_PORT"])
def test_ports_must_be_in_range(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "70000")

    with pytest.raises(ValidationError, match=name.lower()):
        Settings(_env_file=None)


def test_python_m_app_runs_uvicorn_with_configured_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import __main__ as entrypoint

    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs)))
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9001")

    entrypoint.main()

    assert calls == [
        ("app.main:app", {"host": "0.0.0.0", "port": 9001, "log_level": "info", "access_log": False})
    ]


# --- Production secrets -------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("password", [None, "", EXAMPLE_POSTGRES_PASSWORD])
def test_production_requires_a_real_database_password(
    monkeypatch: pytest.MonkeyPatch, password: str | None
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    if password is None:
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("POSTGRES_PASSWORD", password)

    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD must"):
        Settings(_env_file=None)


def test_production_with_a_password_is_valid(monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POSTGRES_PASSWORD", SECRET)
    # Production also requires the JWT key pair (Phase 1.3); see the "Access-token keys" tests below.
    monkeypatch.setenv("JWT_PRIVATE_KEY", jwt_keys.private_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY", jwt_keys.public_pem)

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.postgres_password is not None
    assert settings.postgres_password.get_secret_value() == SECRET


@pytest.mark.security
def test_validation_errors_do_not_expose_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    # Model-level error: pydantic would otherwise print every input value, including the password.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POSTGRES_PASSWORD", EXAMPLE_POSTGRES_PASSWORD)
    with pytest.raises(ValidationError) as model_error:
        Settings(_env_file=None)

    # Field-level error on another field while a real secret is set.
    monkeypatch.setenv("POSTGRES_PASSWORD", SECRET)
    monkeypatch.setenv("POSTGRES_PORT", "not-a-port")
    with pytest.raises(ValidationError) as field_error:
        Settings(_env_file=None)

    # Errors still say what is wrong, but never echo inputs (truncation alone could mask a partial leak).
    assert "POSTGRES_PASSWORD must not be the .env.example placeholder" in str(model_error.value)
    assert "postgres_port" in str(field_error.value)
    for error in (model_error.value, field_error.value):
        assert "input_value" not in str(error)
        assert EXAMPLE_POSTGRES_PASSWORD not in str(error)
        assert SECRET not in str(error)


# --- Access-token keys and lifetime (Phase 1.3) -------------------------------------------------------------


def _key_lines(pem: str) -> list[str]:
    """The base64 body lines of a PEM: what any leak of the key material would contain."""
    return [line for line in pem.splitlines() if line and not line.startswith("-----")]


def _encrypted(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"fake-passphrase"),
    ).decode()


def _elliptic_curve_pair() -> RsaKeyPair:
    # A valid key pair of the wrong type: RS256 needs RSA. (RsaKeyPair is only the (private, public) container.)
    key = ec.generate_private_key(ec.SECP256R1())
    return RsaKeyPair(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


def test_jwt_settings_defaults(settings: Settings) -> None:
    assert settings.jwt_private_key is None
    assert settings.jwt_public_key is None
    assert settings.access_token_lifetime_minutes == 15


@pytest.mark.security
def test_jwt_settings_read_from_environment_with_the_private_key_masked(
    monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair
) -> None:
    monkeypatch.setenv("JWT_PRIVATE_KEY", jwt_keys.private_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY", jwt_keys.public_pem)
    monkeypatch.setenv("ACCESS_TOKEN_LIFETIME_MINUTES", "5")

    settings = Settings(_env_file=None)

    assert settings.jwt_private_key is not None
    assert settings.jwt_private_key.get_secret_value() == jwt_keys.private_pem
    assert settings.jwt_public_key == jwt_keys.public_pem
    assert settings.access_token_lifetime_minutes == 5
    for line in _key_lines(jwt_keys.private_pem):
        assert line not in repr(settings)
        assert line not in settings.model_dump_json()


def test_documented_development_setup_issues_and_verifies_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair
) -> None:
    # The format the documented procedure appends to backend/.env: each PEM as a double-quoted multi-line value,
    # without the final newline (shell command substitution drops it).
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        f'JWT_PRIVATE_KEY="{jwt_keys.private_pem.rstrip()}"\nJWT_PUBLIC_KEY="{jwt_keys.public_pem.rstrip()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setitem(Settings.model_config, "env_file", dotenv)
    monkeypatch.setenv("ENVIRONMENT", "development")
    user = AuthenticatedUser(
        user_id=uuid4(),
        company_id=uuid4(),
        email="dev@example.com",
        full_name="Dev User",
        role=UserRole.STAFF,
        status=UserStatus.ACTIVE,
    )

    settings = Settings()

    assert settings.environment == "development"
    assert settings.jwt_public_key == jwt_keys.public_pem.rstrip()
    assert decode_access_token(issue_access_token(user, settings), settings).user_id == user.user_id


@pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
def test_blank_jwt_key_variables_mean_not_configured(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JWT_PRIVATE_KEY", value)
    monkeypatch.setenv("JWT_PUBLIC_KEY", value)

    settings = Settings(_env_file=None)

    assert settings.jwt_private_key is None
    assert settings.jwt_public_key is None


@pytest.mark.security
@pytest.mark.parametrize(
    "configured", [(), ("JWT_PRIVATE_KEY",), ("JWT_PUBLIC_KEY",)], ids=["neither", "private-only", "public-only"]
)
def test_production_requires_the_jwt_key_pair(
    monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair, configured: tuple[str, ...]
) -> None:
    values = {"JWT_PRIVATE_KEY": jwt_keys.private_pem, "JWT_PUBLIC_KEY": jwt_keys.public_pem}
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POSTGRES_PASSWORD", SECRET)
    for name in configured:
        monkeypatch.setenv(name, values[name])

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert "JWT_PRIVATE_KEY and JWT_PUBLIC_KEY must be set when ENVIRONMENT=production" in str(excinfo.value)
    for line in _key_lines(jwt_keys.private_pem):
        assert line not in str(excinfo.value)


@pytest.mark.parametrize("name", ["JWT_PRIVATE_KEY", "JWT_PUBLIC_KEY"])
def test_jwt_keys_must_be_configured_together(monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair, name: str) -> None:
    monkeypatch.setenv(name, {"JWT_PRIVATE_KEY": jwt_keys.private_pem, "JWT_PUBLIC_KEY": jwt_keys.public_pem}[name])

    with pytest.raises(ValidationError, match="JWT_PRIVATE_KEY and JWT_PUBLIC_KEY must be set together"):
        Settings(_env_file=None)


@pytest.mark.security
@pytest.mark.parametrize(
    ("make_pair", "message"),
    [
        (lambda keys: RsaKeyPair("not-a-key", keys.public_pem), "JWT_PRIVATE_KEY must be an unencrypted PEM RSA"),
        (lambda keys: RsaKeyPair(keys.public_pem, keys.public_pem), "JWT_PRIVATE_KEY must be an unencrypted PEM RSA"),
        (lambda keys: RsaKeyPair(_encrypted(keys.private_pem), keys.public_pem), "JWT_PRIVATE_KEY must be an unencrypted"),
        (lambda keys: RsaKeyPair(keys.private_pem, "not-a-key"), "JWT_PUBLIC_KEY must be a PEM RSA public key"),
        (lambda keys: RsaKeyPair(keys.private_pem, keys.private_pem), "JWT_PUBLIC_KEY must be a PEM RSA public key"),
        (
            lambda keys: RsaKeyPair(keys.private_pem, generate_rsa_key_pair().public_pem),
            "JWT_PUBLIC_KEY does not match JWT_PRIVATE_KEY",
        ),
        (lambda keys: generate_rsa_key_pair(1024), "JWT keys must be RSA keys of at least 2048 bits"),
        (lambda keys: _elliptic_curve_pair(), "JWT_PRIVATE_KEY and JWT_PUBLIC_KEY must be RSA keys (RS256)"),
    ],
    ids=[
        "private-not-pem",
        "public-key-as-private",
        "encrypted-private",
        "public-not-pem",
        "private-key-as-public",
        "mismatched-pair",
        "rsa-1024",
        "elliptic-curve",
    ],
)
def test_unusable_jwt_keys_are_rejected_without_exposing_key_material(
    monkeypatch: pytest.MonkeyPatch,
    jwt_keys: RsaKeyPair,
    make_pair: Callable[[RsaKeyPair], RsaKeyPair],
    message: str,
) -> None:
    pair = make_pair(jwt_keys)
    monkeypatch.setenv("JWT_PRIVATE_KEY", pair.private_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY", pair.public_pem)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)
    error = str(excinfo.value)

    assert message in error
    assert "input_value" not in error
    assert "-----BEGIN" not in error
    for line in _key_lines(pair.private_pem) + _key_lines(pair.public_pem):
        assert line not in error


@pytest.mark.parametrize("minutes", ["1", "60"])
def test_access_token_lifetime_of_1_to_60_minutes_is_accepted(monkeypatch: pytest.MonkeyPatch, minutes: str) -> None:
    monkeypatch.setenv("ACCESS_TOKEN_LIFETIME_MINUTES", minutes)

    assert Settings(_env_file=None).access_token_lifetime_minutes == int(minutes)


@pytest.mark.security
@pytest.mark.parametrize("environment", ["development", "production"])
@pytest.mark.parametrize("minutes", ["0", "-5", "61", "1440", "fifteen"])
def test_access_token_lifetime_outside_1_to_60_minutes_is_rejected(
    monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair, environment: str, minutes: str
) -> None:
    # The cap holds in every environment, so production cannot silently run with long-lived tokens.
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("POSTGRES_PASSWORD", SECRET)
    monkeypatch.setenv("JWT_PRIVATE_KEY", jwt_keys.private_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY", jwt_keys.public_pem)
    monkeypatch.setenv("ACCESS_TOKEN_LIFETIME_MINUTES", minutes)

    with pytest.raises(ValidationError, match="access_token_lifetime_minutes"):
        Settings(_env_file=None)


@pytest.mark.security
@pytest.mark.parametrize("path", [BACKEND_DIR / ".env.example", BACKEND_DIR.parent / ".env.example"], ids=["backend", "root"])
def test_env_examples_contain_no_key_material(path: Path) -> None:
    text = path.read_text(encoding="utf-8")

    assert "-----" not in text and "BEGIN" not in text
    assert not [line for line in text.splitlines() if line.startswith(("JWT_PRIVATE_KEY=", "JWT_PUBLIC_KEY="))]


def test_backend_env_example_configures_no_keys_and_the_default_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(Settings.model_config, "env_file", BACKEND_DIR / ".env.example")
    monkeypatch.setenv("ENVIRONMENT", "development")

    settings = Settings()

    assert settings.jwt_private_key is None
    assert settings.jwt_public_key is None
    assert settings.access_token_lifetime_minutes == 15


# --- Single-company demo access (D-036) ------------------------------------------------------------------------


def test_demo_access_is_off_by_default_and_in_the_example(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings.demo_company_slug is None and settings.demo_user_email is None

    monkeypatch.setitem(Settings.model_config, "env_file", BACKEND_DIR / ".env.example")
    monkeypatch.setenv("ENVIRONMENT", "development")
    example = Settings()

    assert example.demo_company_slug is None and example.demo_user_email is None


def test_demo_access_reads_both_settings_and_normalises_the_company_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEMO_COMPANY_SLUG", " Demo-Health ")
    monkeypatch.setenv("DEMO_USER_EMAIL", " admin@demo-health.example ")

    settings = Settings(_env_file=None)

    assert settings.demo_company_slug == "demo-health"
    assert settings.demo_user_email == "admin@demo-health.example"


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_demo_settings_mean_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DEMO_COMPANY_SLUG", value)
    monkeypatch.setenv("DEMO_USER_EMAIL", value)

    settings = Settings(_env_file=None)

    assert settings.demo_company_slug is None and settings.demo_user_email is None


@pytest.mark.parametrize("name", ["DEMO_COMPANY_SLUG", "DEMO_USER_EMAIL"])
def test_demo_settings_must_be_set_together(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setenv(name, "demo-health" if name == "DEMO_COMPANY_SLUG" else "admin@demo-health.example")

    with pytest.raises(ValidationError, match="must be set together"):
        Settings(_env_file=None)


@pytest.mark.parametrize("slug", ["demo health", "demo--health", "-demo", "demo/../x", "demo_health"])
def test_the_demo_company_code_must_be_a_company_code(monkeypatch: pytest.MonkeyPatch, slug: str) -> None:
    with pytest.raises(ValidationError, match="DEMO_COMPANY_SLUG must be a company code"):
        Settings(_env_file=None, demo_company_slug=slug, demo_user_email="admin@demo-health.example")


@pytest.mark.security
def test_demo_access_is_refused_in_production(monkeypatch: pytest.MonkeyPatch, jwt_keys: RsaKeyPair) -> None:
    # Everything else production needs is valid, so only the demo settings are refused.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("POSTGRES_PASSWORD", SECRET)
    monkeypatch.setenv("JWT_PRIVATE_KEY", jwt_keys.private_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY", jwt_keys.public_pem)
    monkeypatch.setenv("DEMO_COMPANY_SLUG", "demo-health")
    monkeypatch.setenv("DEMO_USER_EMAIL", "admin@demo-health.example")

    with pytest.raises(ValidationError, match="not allowed when ENVIRONMENT=production"):
        Settings(_env_file=None)
