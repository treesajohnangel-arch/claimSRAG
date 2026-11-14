"""Application configuration, loaded from environment variables and an optional backend/.env file.

Secrets are ``SecretStr``: they are masked in repr/str/JSON. Read them with ``.get_secret_value()`` only at the
point of use, and never log them.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal, Self

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

# Placeholder password used in the .env.example files. Never acceptable in production.
EXAMPLE_POSTGRES_PASSWORD = "change-me-local-dev-only"

# Access tokens (D-017): RS256 needs an RSA key pair of at least this size. The lifetime is capped in every
# environment, so a long-lived token cannot be configured by accident.
MIN_JWT_RSA_KEY_BITS = 2048
DEFAULT_ACCESS_TOKEN_LIFETIME_MINUTES = 15
MAX_ACCESS_TOKEN_LIFETIME_MINUTES = 60
# The local LLM (Phase 8, D-030) must run on this machine or the Compose network: questions and policy text never
# leave it. Only these host names are accepted for OLLAMA_BASE_URL.
LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "ollama", "host.docker.internal"})
_LLM_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}")


class Settings(BaseSettings):
    """ClaimsRAG settings. Each field is read from the upper-case environment variable of the same name.

    Precedence: explicit arguments > environment variables > backend/.env > defaults.
    When the process environment has ENVIRONMENT=test (pytest sets it), backend/.env is not read, so tests never
    depend on a developer's local file.
    """

    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must not echo input values: a model-level error would otherwise print every input,
        # including POSTGRES_PASSWORD in plain text.
        hide_input_in_errors=True,
    )

    # Application
    app_name: str = "ClaimsRAG"
    app_version: str = "0.1.0"
    environment: Literal["development", "test", "production"] = "development"
    # Not passed to FastAPI/Starlette: their debug mode returns stack traces to clients.
    debug: bool = False
    # Level for ClaimsRAG's own loggers ("app.*"). Any case is accepted and stored upper-case.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Server: bind address for `python -m app` (the uvicorn CLI takes --host/--port instead).
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)

    # PostgreSQL connection settings. Configuration only: the application does not connect to the database yet.
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)
    postgres_db: str = "claimsrag"
    postgres_user: str = "claimsrag"
    # Secret, with no default. Required in production (see below).
    postgres_password: SecretStr | None = None

    # Access-token signing (D-017). PEM text: an unencrypted RSA private key (secret) and its public key. Both or
    # neither; required in production. Without them, the token service refuses to issue or verify tokens.
    jwt_private_key: SecretStr | None = None
    jwt_public_key: str | None = None
    access_token_lifetime_minutes: int = Field(
        default=DEFAULT_ACCESS_TOKEN_LIFETIME_MINUTES, ge=1, le=MAX_ACCESS_TOKEN_LIFETIME_MINUTES
    )

    # Grounded answers (Phase 8, D-030): a local Ollama server. The URL must name a local host (LOCAL_LLM_HOSTS).
    # Temperature, seed and context size are fixed in code, not configuration, so answers stay deterministic.
    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen2.5:3b"
    llm_timeout_seconds: int = Field(default=120, ge=1, le=600)
    llm_max_output_tokens: int = Field(default=400, ge=16, le=2048)

    # Uploaded policy PDFs (D-035): the server-side directory LocalDocumentStorage writes them to, and the largest
    # request body the API accepts (a PDF upload is the largest legitimate body). The directory is never chosen by a
    # client; files in it are named only by server-generated storage references.
    document_storage_dir: Path = BACKEND_DIR / ".document_storage"
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)

    # Single-company demo access (D-036). Off unless both are set: then POST /auth/demo-session issues a normal access
    # token for exactly this one active user of this one company, without a password, so the frontend opens without a
    # sign-in screen. Never allowed when ENVIRONMENT=production.
    demo_company_slug: str | None = None
    demo_user_email: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        if os.environ.get("ENVIRONMENT") == "test":
            return init_settings, env_settings, file_secret_settings
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @field_validator("ollama_base_url")
    @classmethod
    def _local_llm_only(cls, value: str) -> str:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or parts.hostname not in LOCAL_LLM_HOSTS or parts.username
                or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}):
            raise ValueError("ollama_base_url must be an http(s) URL of a local host, with no path or credentials")
        return value.rstrip("/")

    @field_validator("llm_model")
    @classmethod
    def _plain_model_name(cls, value: str) -> str:
        if not _LLM_MODEL_NAME.fullmatch(value):
            raise ValueError("llm_model must be a plain Ollama model name")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_case_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("jwt_private_key", "jwt_public_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value: object) -> object:
        # An empty variable (e.g. `JWT_PRIVATE_KEY=`) means "not configured", not "malformed key".
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _require_real_secrets_in_production(self) -> Self:
        if self.environment == "production":
            password = self.postgres_password.get_secret_value() if self.postgres_password else ""
            if not password:
                raise ValueError("POSTGRES_PASSWORD must be set when ENVIRONMENT=production")
            if password == EXAMPLE_POSTGRES_PASSWORD:
                raise ValueError("POSTGRES_PASSWORD must not be the .env.example placeholder when ENVIRONMENT=production")
            if self.jwt_private_key is None or self.jwt_public_key is None:
                raise ValueError("JWT_PRIVATE_KEY and JWT_PUBLIC_KEY must be set when ENVIRONMENT=production")
        return self

    @field_validator("demo_company_slug", "demo_user_email", mode="before")
    @classmethod
    def _blank_demo_setting_is_unset(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _check_demo_access(self) -> Self:
        """Demo access is all or nothing, names a well-formed company code, and never runs in production."""
        if self.demo_company_slug is None and self.demo_user_email is None:
            return self
        if self.demo_company_slug is None or self.demo_user_email is None:
            raise ValueError("DEMO_COMPANY_SLUG and DEMO_USER_EMAIL must be set together")
        if self.environment == "production":
            raise ValueError("Demo access (DEMO_COMPANY_SLUG, DEMO_USER_EMAIL) is not allowed when ENVIRONMENT=production")
        self.demo_company_slug = self.demo_company_slug.strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", self.demo_company_slug):
            raise ValueError("DEMO_COMPANY_SLUG must be a company code (lower-case letters, digits, single hyphens)")
        self.demo_user_email = self.demo_user_email.strip()
        return self

    @model_validator(mode="after")
    def _check_jwt_key_pair(self) -> Self:
        """Fail at startup, not at the first login, if the configured keys cannot sign and verify RS256 tokens.

        Error messages name the setting but never include key material.
        """
        if self.jwt_private_key is None and self.jwt_public_key is None:
            return self
        if self.jwt_private_key is None or self.jwt_public_key is None:
            raise ValueError("JWT_PRIVATE_KEY and JWT_PUBLIC_KEY must be set together")
        try:
            private_key = serialization.load_pem_private_key(
                self.jwt_private_key.get_secret_value().encode(), password=None
            )
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError("JWT_PRIVATE_KEY must be an unencrypted PEM RSA private key") from None
        try:
            public_key = serialization.load_pem_public_key(self.jwt_public_key.encode())
        except (ValueError, UnsupportedAlgorithm):
            raise ValueError("JWT_PUBLIC_KEY must be a PEM RSA public key") from None
        if not isinstance(private_key, rsa.RSAPrivateKey) or not isinstance(public_key, rsa.RSAPublicKey):
            raise ValueError("JWT_PRIVATE_KEY and JWT_PUBLIC_KEY must be RSA keys (RS256)")
        if private_key.key_size < MIN_JWT_RSA_KEY_BITS:
            raise ValueError(f"JWT keys must be RSA keys of at least {MIN_JWT_RSA_KEY_BITS} bits")
        if private_key.public_key().public_numbers() != public_key.public_numbers():
            raise ValueError("JWT_PUBLIC_KEY does not match JWT_PRIVATE_KEY")
        return self
