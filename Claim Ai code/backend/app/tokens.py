"""Access tokens: a short-lived, RS256-signed JWT for an already authenticated user (D-017).

``issue_access_token()`` turns the ``AuthenticatedUser`` returned by ``authenticate()`` into a signed token.
``decode_access_token()`` verifies a token and returns the identity it represents.

This is authentication, not authorization. Neither function checks passwords or queries the database. A valid token
proves only that this server signed it and that it has not expired. The user's current status, company status and
role are not re-checked here; that belongs to later authorization work. There are no refresh tokens, and tokens
cannot be revoked before they expire.

Every invalid token raises the same AccessTokenError. The reason is logged as an exception class name only, never
with the token, its claims or key material.
"""

import logging
import uuid
from datetime import UTC, datetime
from functools import cache
from uuid import UUID

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes
from pydantic import BaseModel, ConfigDict, StrictInt, ValidationError

from app.auth import AuthenticatedUser
from app.config import Settings
from app.models import UserRole

logger = logging.getLogger(__name__)

# The only algorithm used to sign, and the only one accepted when verifying. It is fixed here, never taken from the
# token header and not an environment setting, so neither a token nor a misconfiguration can select another one.
ALGORITHM = "RS256"
REQUIRED_CLAIMS = ("sub", "company_id", "role", "iat", "exp", "jti")
GENERIC_TOKEN_FAILURE_MESSAGE = "Invalid or expired token"


class AccessTokenError(Exception):
    """Raised for every invalid access token, always with the same generic message and no reason."""

    def __init__(self) -> None:
        super().__init__(GENERIC_TOKEN_FAILURE_MESSAGE)


class TokenConfigurationError(RuntimeError):
    """The JWT keys are not configured. A server problem, not an invalid token."""


class TokenIdentity(BaseModel):
    """The authenticated identity an access token represents: no credentials and no personal data."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    company_id: UUID
    role: UserRole


class _AccessTokenClaims(BaseModel):
    """The exact claim types this service issues. PyJWT alone would accept, for example, a string or float `exp`
    or a list `role`."""

    sub: UUID
    company_id: UUID
    role: UserRole
    iat: StrictInt
    exp: StrictInt
    jti: UUID


def issue_access_token(user: AuthenticatedUser, settings: Settings, *, now: datetime | None = None) -> str:
    """Return a signed access token for an identity that ``authenticate()`` has already verified.

    The claims come only from that identity, never from client input. ``now`` (timezone-aware) sets the issue time.
    """
    if not isinstance(user, AuthenticatedUser):
        raise TypeError("Access tokens are issued only for an AuthenticatedUser")
    if settings.jwt_private_key is None:
        raise TokenConfigurationError("JWT_PRIVATE_KEY is not configured")

    issued_at = int((now or datetime.now(UTC)).timestamp())
    claims = {
        "sub": str(user.user_id),
        "company_id": str(user.company_id),
        "role": user.role.value,
        "iat": issued_at,
        "exp": issued_at + settings.access_token_lifetime_minutes * 60,
        "jti": str(uuid.uuid4()),  # random (122 bits), never sequential
    }
    token = jwt.encode(claims, _signing_key(settings.jwt_private_key.get_secret_value()), algorithm=ALGORITHM)
    logger.info("access token issued user_id=%s jti=%s", user.user_id, claims["jti"])
    return token


def decode_access_token(token: str, settings: Settings) -> TokenIdentity:
    """Verify the signature (RS256 only), the required claims, their types and expiry; return the identity.

    Raises AccessTokenError for any invalid token.
    """
    if settings.jwt_public_key is None:
        raise TokenConfigurationError("JWT_PUBLIC_KEY is not configured")
    try:
        payload = jwt.decode(
            token, settings.jwt_public_key, algorithms=[ALGORITHM], options={"require": list(REQUIRED_CLAIMS)}
        )
        claims = _AccessTokenClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError) as exc:
        logger.info("access token rejected reason=%s", type(exc).__name__)
        # `from None`: the library's exception, which may describe the token, does not travel with the error.
        raise AccessTokenError from None
    return TokenIdentity(user_id=claims.sub, company_id=claims.company_id, role=claims.role)


@cache
def _signing_key(private_key_pem: str) -> PrivateKeyTypes:
    # Loading validates the RSA key, which takes about 35 ms, so each configured key is loaded once, not per token.
    return serialization.load_pem_private_key(private_key_pem.encode(), password=None)
