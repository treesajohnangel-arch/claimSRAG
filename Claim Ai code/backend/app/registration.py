"""Company registration (D-035): create a company (a tenant) and its first COMPANY_ADMIN, before any tenant exists.

Like ``app.auth.authenticate``, this runs before authentication, so it cannot use ``TenantDatabase``: there is no
tenant yet. It is one SQL statement, so the company and its admin are created together or not at all:

    WITH company AS (INSERT INTO companies ... RETURNING id)
    INSERT INTO users (company_id, ..., role) SELECT company.id, ..., 'COMPANY_ADMIN' FROM company

- **The tenant is never client-chosen.** The new user's ``company_id`` is the id the database just generated for
  the new company, inside the same statement. No caller value can name an existing company.
- **The role is fixed.** It is the literal ``'COMPANY_ADMIN'`` in the SQL; there is no role parameter.
- **Inputs are validated here** (not only by the database's CHECK constraints), with user-facing messages per field:
  the company name and code, the admin's name and email (``normalize_email``, as at sign-in), and the password policy
  (``app.passwords.password_problem``). The password is hashed with ``hash_password`` (Argon2id).
- **Errors:** invalid input raises ``RegistrationInvalid`` (field → message). A company code that is already
  registered raises ``CompanyCodeTaken``. Other database errors propagate; the caller's transaction rolls back.
- **Nothing sensitive is logged or returned:** the result has no ids or hash; the log has the new company and user
  ids only.
"""

import logging
import re
from typing import Any

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict

from app.emails import InvalidEmailError, normalize_email
from app.passwords import hash_password, password_problem

logger = logging.getLogger(__name__)

COMPANY_SLUG = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")  # companies_slug_check (migration 0001)
MAX_COMPANY_NAME = 200
MAX_COMPANY_SLUG = 63
MAX_FULL_NAME = 200

_REGISTER_SQL = """
    WITH company AS (
        INSERT INTO companies (name, slug) VALUES (%(company_name)s, %(company_slug)s) RETURNING id, name, slug
    )
    INSERT INTO users (company_id, email, password_hash, full_name, role)
    SELECT company.id, %(email)s, %(password_hash)s, %(full_name)s, 'COMPANY_ADMIN' FROM company
    RETURNING id, company_id, email, full_name, role,
              (SELECT name FROM company) AS company_name, (SELECT slug FROM company) AS company_slug
"""


class RegistrationInvalid(Exception):
    """Some fields are not acceptable. ``fields`` maps each field name to a user-facing message."""

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields
        super().__init__("Registration details are not valid: " + ", ".join(sorted(fields)))


class CompanyCodeTaken(Exception):
    """A company with this code (slug) is already registered."""


class RegisteredCompany(BaseModel):
    """What registration created. No ids and no password hash."""

    model_config = ConfigDict(frozen=True)

    company_name: str
    company_slug: str
    admin_full_name: str
    admin_email: str
    admin_role: str


def _validated(company_name: str, company_slug: str, admin_full_name: str, admin_email: str,
               admin_password: str) -> dict[str, str]:
    problems: dict[str, str] = {}
    name, slug, full_name = company_name.strip(), company_slug.strip().lower(), admin_full_name.strip()
    if not name:
        problems["company_name"] = "Enter the company name."
    elif len(name) > MAX_COMPANY_NAME:
        problems["company_name"] = f"Use at most {MAX_COMPANY_NAME} characters."
    if not slug:
        problems["company_slug"] = "Enter a company code."
    elif len(slug) > MAX_COMPANY_SLUG or not COMPANY_SLUG.fullmatch(slug):
        problems["company_slug"] = (f"Use lower-case letters, digits and single hyphens, at most {MAX_COMPANY_SLUG} "
                                    "characters, for example acme-health.")
    if not full_name:
        problems["admin_full_name"] = "Enter the administrator's name."
    elif len(full_name) > MAX_FULL_NAME:
        problems["admin_full_name"] = f"Use at most {MAX_FULL_NAME} characters."
    email = ""
    try:
        email = normalize_email(admin_email)
    except InvalidEmailError:
        problems["admin_email"] = "Enter a valid email address."
    problem = password_problem(admin_password)
    if problem:
        problems["admin_password"] = problem
    if problems:
        raise RegistrationInvalid(problems)
    return {"company_name": name, "company_slug": slug, "full_name": full_name, "email": email}


def register_company(conn: psycopg.Connection[Any], *, company_name: str, company_slug: str, admin_full_name: str,
                     admin_email: str, admin_password: str) -> RegisteredCompany:
    """Create a company and its first COMPANY_ADMIN in one statement. The caller owns the transaction."""
    values = _validated(company_name, company_slug, admin_full_name, admin_email, admin_password)
    params = {**values, "password_hash": hash_password(admin_password)}
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            row = cur.execute(_REGISTER_SQL, params).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name == "companies_slug_key":
            logger.info("company registration refused reason=company_code_taken")
            raise CompanyCodeTaken() from None
        raise
    assert row is not None
    logger.info("company registered company_id=%s admin_user_id=%s", row["company_id"], row["id"])
    return RegisteredCompany(company_name=row["company_name"], company_slug=row["company_slug"],
                             admin_full_name=row["full_name"], admin_email=row["email"], admin_role=row["role"])
