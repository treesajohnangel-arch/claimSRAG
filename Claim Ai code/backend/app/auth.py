"""Authentication: are these credentials valid for an active user of an active company?

Credentials are (company slug, email, password). Email is unique only within a company (D-013), so the slug selects
which company's account is checked (D-016). The password must still verify, and the returned company_id always
comes from the stored row.

Authentication is not authorization. This module only establishes who the user is. Sessions/JWT (Phase 1.3),
RBAC and tenant resolution are not implemented here.

Every failure raises the same AuthenticationError. The real reason (unknown account, wrong password, inactive user
or company) is logged server-side by user id only: never the email, password or hash.

``demo_account`` (D-036) is the one lookup without a password: it serves the development-only single-company demo
session, for exactly the user and company named in the server's settings (never by a client). It does not read the
password hash, and refuses an inactive user or company like sign-in does.
"""

import logging
import secrets
from functools import cache
from typing import Any, NoReturn
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from app.emails import InvalidEmailError, normalize_email
from app.models import CompanyStatus, UserRole, UserStatus
from app.passwords import hash_password, verify_password

logger = logging.getLogger(__name__)

GENERIC_FAILURE_MESSAGE = "Invalid credentials"

_LOOKUP_SQL = """
    SELECT u.id, u.company_id, u.email, u.password_hash, u.full_name, u.role, u.status,
           c.status AS company_status
    FROM users AS u
    JOIN companies AS c ON c.id = u.company_id
    WHERE c.slug = %(slug)s AND u.email = %(email)s
"""

_DEMO_LOOKUP_SQL = """
    SELECT u.id, u.company_id, u.email, u.full_name, u.role, u.status,
           c.status AS company_status, c.name AS company_name, c.slug AS company_slug
    FROM users AS u
    JOIN companies AS c ON c.id = u.company_id
    WHERE c.slug = %(slug)s AND u.email = %(email)s
"""


class AuthenticationError(Exception):
    """Raised for every authentication failure, always with the same generic message and no reason."""

    def __init__(self) -> None:
        super().__init__(GENERIC_FAILURE_MESSAGE)


class AuthenticatedUser(BaseModel):
    """Who an authenticated user is. It carries no credentials; later phases build sessions, RBAC and tenant
    resolution on it."""

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    company_id: UUID
    email: str
    full_name: str
    role: UserRole
    status: UserStatus


def authenticate(conn: psycopg.Connection[Any], company_slug: str, email: str, password: str) -> AuthenticatedUser:
    """Return the identity for valid credentials of an active user in an active company, or raise
    AuthenticationError."""
    try:
        normalized_email: str | None = normalize_email(email)
    except InvalidEmailError:
        normalized_email = None

    row = None
    if normalized_email is not None:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(_LOOKUP_SQL, {"slug": company_slug.strip().lower(), "email": normalized_email}).fetchone()

    if row is None:
        # Do the same Argon2id work as for a real account, so the response time does not reveal whether it exists.
        verify_password(password, _dummy_hash())
        _fail("unknown_account", None)
    # The password is checked before status, so an account's status is never revealed without its password.
    if not verify_password(password, row["password_hash"]):
        _fail("wrong_password", row["id"])
    if row["status"] != UserStatus.ACTIVE:
        _fail("inactive_user", row["id"])
    if row["company_status"] != CompanyStatus.ACTIVE:
        _fail("inactive_company", row["id"])

    identity = AuthenticatedUser(
        user_id=row["id"],
        company_id=row["company_id"],
        email=row["email"],
        full_name=row["full_name"],
        role=row["role"],
        status=row["status"],
    )
    logger.info("authentication succeeded user_id=%s company_id=%s", identity.user_id, identity.company_id)
    return identity


class DemoAccountUnavailable(Exception):
    """The configured demo user does not exist, or the user or its company is not active."""


class DemoAccount(BaseModel):
    """The demo session's user, and the name and code of its company (for display only)."""

    model_config = ConfigDict(frozen=True)

    user: AuthenticatedUser
    company_name: str
    company_slug: str


def demo_account(conn: psycopg.Connection[Any], company_slug: str, email: str) -> DemoAccount:
    """Return the configured demo user of the configured company (D-036), or raise DemoAccountUnavailable.

    The slug and email come from the server's settings only. The company id, role and status come from the stored
    rows, exactly as for sign-in.
    """
    try:
        normalized_email = normalize_email(email)
    except InvalidEmailError:
        logger.warning("demo session unavailable reason=invalid_demo_email")
        raise DemoAccountUnavailable from None
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(_DEMO_LOOKUP_SQL, {"slug": company_slug.strip().lower(), "email": normalized_email}).fetchone()
    if row is None:
        logger.warning("demo session unavailable reason=unknown_account")
        raise DemoAccountUnavailable
    if row["status"] != UserStatus.ACTIVE or row["company_status"] != CompanyStatus.ACTIVE:
        reason = "inactive_user" if row["status"] != UserStatus.ACTIVE else "inactive_company"
        logger.warning("demo session unavailable reason=%s user_id=%s", reason, row["id"])
        raise DemoAccountUnavailable
    user = AuthenticatedUser(user_id=row["id"], company_id=row["company_id"], email=row["email"],
                             full_name=row["full_name"], role=row["role"], status=row["status"])
    logger.info("demo session issued user_id=%s company_id=%s", user.user_id, user.company_id)
    return DemoAccount(user=user, company_name=row["company_name"], company_slug=row["company_slug"])


def _fail(reason: str, user_id: UUID | None) -> NoReturn:
    logger.info("authentication failed reason=%s user_id=%s", reason, user_id)
    raise AuthenticationError


@cache
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))
