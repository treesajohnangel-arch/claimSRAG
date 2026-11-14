"""Sign-in, company registration and the demo session (D-032, D-035, D-036): the routes that run before there is a
token.

    POST /auth/login             {company_slug, email, password} → authenticate() (D-016) → issue_access_token()
                                 (D-017) → {access_token, token_type, expires_in, user: {full_name, email, role}}
    POST /auth/register-company  {company_name, company_slug, admin_full_name, admin_email, admin_password}
                                 → register_company() → 201 {company: {name, slug}, admin: {full_name, email, role}}
    POST /auth/demo-session      (no body) → demo_account() for the user and company named by DEMO_USER_EMAIL and
                                 DEMO_COMPANY_SLUG → issue_access_token() → {access_token, token_type, expires_in,
                                 user, company: {name, slug}}; 404 when demo access is not configured (D-036)

- **The public writes.** They happen before there is a token, so they declare no permission. They are exactly the
  entries of ``app.api.authorization.PUBLIC_WRITES``; the startup guard and structural tests pin them.
- **No tenant or role from the client.** The company slug only says whose account the credentials are checked
  against (D-016). The token's company and role come from the stored user row. Registration creates its company and
  admin in one statement, with the role fixed to COMPANY_ADMIN (``app.registration``). The demo session takes no
  input at all: its user and company are the server's settings, which are refused in production.
- **One sign-in failure.** An unknown company or user, a wrong password, or an inactive user or company is the same
  401 "Invalid credentials" (``authenticate`` equalises the timing).
- **Validation never echoes a password.** Fields default to empty strings, so a missing field reaches this code
  instead of FastAPI's 422, which would echo the whole body back. Missing sign-in fields and invalid registration
  fields get a 422 that names the field only (``loc``, ``msg``, ``type``; never ``input``).
- **Nothing sensitive is logged or returned.** ``authenticate`` logs only the outcome and user id, registration only
  the new ids; the request log has method, path and status. Responses carry no company id, password hash or status.
- **Not here:** refresh tokens, logout or revocation (a token lasts ``ACCESS_TOKEN_LIFETIME_MINUTES``), rate limiting
  and lockout (Phase 14).
"""

from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, StrictStr

from app.api.database import database_connection, database_transaction
from app.auth import GENERIC_FAILURE_MESSAGE, AuthenticationError, DemoAccountUnavailable, authenticate, demo_account
from app.models import UserRole
from app.registration import CompanyCodeTaken, RegistrationInvalid, register_company
from app.tokens import TokenConfigurationError, issue_access_token

router = APIRouter(tags=["auth"])

SIGN_IN_UNAVAILABLE = "Sign-in is temporarily unavailable"
COMPANY_CODE_TAKEN = "This company code is already registered"
DEMO_UNAVAILABLE = "The demo workspace is unavailable"


def _invalid_fields(fields: dict[str, str]) -> HTTPException:
    """A 422 in FastAPI's shape that names each field and never repeats its input."""
    detail = [{"loc": ["body", field], "msg": message, "type": "value_error"} for field, message in sorted(fields.items())]
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_slug: StrictStr = ""
    email: StrictStr = ""
    password: StrictStr = ""


class SignedInUser(BaseModel):
    full_name: str
    email: str
    role: UserRole


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until the token expires
    user: SignedInUser


@router.post("/auth/login")
def login(body: LoginRequest, conn: Annotated[psycopg.Connection[Any], Depends(database_connection)],
          request: Request) -> LoginResponse:
    missing = {field: "Field required" for field in ("company_slug", "email", "password") if not getattr(body, field)}
    if missing:
        raise _invalid_fields(missing)
    try:
        user = authenticate(conn, body.company_slug, body.email, body.password)
    except AuthenticationError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=GENERIC_FAILURE_MESSAGE) from None
    settings = request.app.state.settings
    try:
        token = issue_access_token(user, settings)
    except TokenConfigurationError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SIGN_IN_UNAVAILABLE) from None
    return LoginResponse(access_token=token, expires_in=settings.access_token_lifetime_minutes * 60,
                         user=SignedInUser(full_name=user.full_name, email=user.email, role=user.role))


class CompanyRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: StrictStr = ""
    company_slug: StrictStr = ""
    admin_full_name: StrictStr = ""
    admin_email: StrictStr = ""
    admin_password: StrictStr = ""


class RegisteredCompanyInfo(BaseModel):
    name: str
    slug: str


class RegistrationResponse(BaseModel):
    company: RegisteredCompanyInfo
    admin: SignedInUser


@router.post("/auth/register-company", status_code=status.HTTP_201_CREATED)
def register(body: CompanyRegistration,
             conn: Annotated[psycopg.Connection[Any], Depends(database_transaction, scope="function")]
             ) -> RegistrationResponse:
    try:
        created = register_company(conn, company_name=body.company_name, company_slug=body.company_slug,
                                   admin_full_name=body.admin_full_name, admin_email=body.admin_email,
                                   admin_password=body.admin_password)
    except RegistrationInvalid as exc:
        raise _invalid_fields(exc.fields) from None
    except CompanyCodeTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=COMPANY_CODE_TAKEN) from None
    return RegistrationResponse(
        company=RegisteredCompanyInfo(name=created.company_name, slug=created.company_slug),
        admin=SignedInUser(full_name=created.admin_full_name, email=created.admin_email,
                           role=UserRole(created.admin_role)))



class DemoCompany(BaseModel):
    name: str
    slug: str


class DemoSessionResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int  # seconds until the token expires
    user: SignedInUser
    company: DemoCompany


@router.post("/auth/demo-session")
def demo_session(conn: Annotated[psycopg.Connection[Any], Depends(database_connection)],
                 request: Request) -> DemoSessionResponse:
    """A normal access token for the one configured demo user (D-036). Off (404, like a missing route) unless both
    demo settings are set, which config refuses in production."""
    settings = request.app.state.settings
    if settings.demo_company_slug is None or settings.demo_user_email is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")
    try:
        account = demo_account(conn, settings.demo_company_slug, settings.demo_user_email)
        token = issue_access_token(account.user, settings)
    except (DemoAccountUnavailable, TokenConfigurationError):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=DEMO_UNAVAILABLE) from None
    user = account.user
    return DemoSessionResponse(access_token=token, expires_in=settings.access_token_lifetime_minutes * 60,
                               user=SignedInUser(full_name=user.full_name, email=user.email, role=user.role),
                               company=DemoCompany(name=account.company_name, slug=account.company_slug))
