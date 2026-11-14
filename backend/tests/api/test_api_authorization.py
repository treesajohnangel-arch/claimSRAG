"""API tests for the API authorization boundary: a protected route requires a valid identity (401) and its declared
permission (403), and takes the role and the company only from the verified token.

The protected routes here are test-only. Each one uses the full chain a future business route will use:
``permission_required(p)`` (authentication + RBAC) and ``current_tenant`` (tenant resolution). Expected results are
derived from the real ROLE_PERMISSIONS. Phase 1.3/1.4 tests already cover the token cryptography and the 1.4 matrix;
this module adds the protected-route behaviour.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Any

import jwt
import pytest
from fastapi import APIRouter, Body, Cookie, Depends, FastAPI, Header
from fastapi.testclient import TestClient

import app.api.authorization as authorization
import app.api.tenancy as tenancy_api
import app.main as main
import app.rbac as rbac
from app.api.authorization import (
    NOT_AUTHENTICATED_MESSAGE,
    PUBLIC_PATHS,
    PUBLIC_WRITES,
    current_identity,
    permission_required,
    routes_missing_permission,
)
from app.api.tenancy import current_tenant
from app.auth import AuthenticatedUser
from app.config import Settings
from app.main import create_app
from app.models import UserRole, UserStatus
from app.rbac import FORBIDDEN_MESSAGE, ROLE_PERMISSIONS, Permission, require_permission
from app.tenancy import TenantIdentity
from app.tokens import ALGORITHM, GENERIC_TOKEN_FAILURE_MESSAGE, TokenIdentity, issue_access_token
from tests.conftest import RsaKeyPair

COMPANY_A, COMPANY_B = uuid.uuid4(), uuid.uuid4()
CASES = [(role, permission, permission in ROLE_PERMISSIONS[role]) for role in UserRole for permission in Permission]
CASE_IDS = [f"{role.value}-{permission.value}" for role, permission, _ in CASES]
ADMIN_ONLY = sorted(ROLE_PERMISSIONS[UserRole.COMPANY_ADMIN] - ROLE_PERMISSIONS[UserRole.STAFF])


def _path(permission: Permission) -> str:
    return f"/_protected_test/{permission.name.lower()}"


def _add_protected_route(app: FastAPI, permission: Permission) -> None:
    @app.post(_path(permission))
    def protected(
        identity: Annotated[TokenIdentity, Depends(permission_required(permission))],
        tenant: Annotated[TenantIdentity, Depends(current_tenant)],
        role: str | None = None,
        company_id: str | None = None,
        x_role: Annotated[str | None, Header()] = None,
        x_company_id: Annotated[str | None, Header()] = None,
        cookie_role: Annotated[str | None, Cookie(alias="role")] = None,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, str]:
        # Client role/company values are accepted only to show they influence nothing.
        return {"user_id": str(identity.user_id), "role": identity.role.value, "tenant": str(tenant.company_id)}


def _protected_app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    for permission in Permission:
        _add_protected_route(app, permission)

    @app.post("/_service_check_test")
    def service_check(identity: Annotated[TokenIdentity, Depends(current_identity)]) -> dict[str, bool]:
        # A permission check made by service code inside the route, not by a dependency.
        require_permission(identity, Permission.POLICY_MANAGE)
        return {"ok": True}

    return app


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def protected_app(token_settings: Settings) -> FastAPI:
    return _protected_app(token_settings)


@pytest.fixture
def client(protected_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(protected_app) as test_client:
        yield test_client


def _token(settings: Settings, role: UserRole = UserRole.STAFF, company_id: uuid.UUID = COMPANY_A, **kwargs: Any) -> str:
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=company_id, email="user@acme.example",
                             full_name="Test User", role=role, status=UserStatus.ACTIVE)
    return issue_access_token(user, settings, **kwargs)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Allowed and denied, derived from ROLE_PERMISSIONS --------------------------------------------------------


def test_documented_matrix_is_the_real_mapping() -> None:
    assert ROLE_PERMISSIONS[UserRole.COMPANY_ADMIN] == frozenset(Permission)
    assert ROLE_PERMISSIONS[UserRole.STAFF] == {Permission.POLICY_READ, Permission.SEARCH_USE, Permission.CHAT_USE}


@pytest.mark.security
@pytest.mark.parametrize(("role", "permission", "allowed"), CASES, ids=CASE_IDS)
def test_protected_route_matrix(
    client: TestClient, token_settings: Settings, role: UserRole, permission: Permission, allowed: bool
) -> None:
    response = client.post(_path(permission), headers=_bearer(_token(token_settings, role)))

    if allowed:
        assert response.status_code == 200
        assert response.json()["role"] == role.value
        assert response.json()["tenant"] == str(COMPANY_A)
    else:
        assert response.status_code == 403
        assert response.json() == {"detail": FORBIDDEN_MESSAGE}


# --- Authentication failures stop before authorization and tenant resolution ----------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    ("authorization_header", "detail"),
    [
        (None, NOT_AUTHENTICATED_MESSAGE),
        ("Bearer", NOT_AUTHENTICATED_MESSAGE),
        ("Token abc", NOT_AUTHENTICATED_MESSAGE),
        ("Bearer a.b", GENERIC_TOKEN_FAILURE_MESSAGE),
        ("Bearer not a token", GENERIC_TOKEN_FAILURE_MESSAGE),
        ("expired", GENERIC_TOKEN_FAILURE_MESSAGE),
    ],
    ids=["missing", "scheme-only", "other-scheme", "malformed", "spaces", "expired"],
)
def test_missing_malformed_invalid_or_expired_authentication_is_401_before_rbac_or_tenant(
    client: TestClient, token_settings: Settings, monkeypatch: pytest.MonkeyPatch, authorization_header: Any,
    detail: str,
) -> None:
    reached: list[str] = []
    monkeypatch.setattr(authorization, "require_permission", lambda *a: reached.append("rbac"))
    monkeypatch.setattr(tenancy_api, "resolve_tenant", lambda *a: reached.append("tenant"))
    if authorization_header == "expired":
        authorization_header = f"Bearer {_token(token_settings, UserRole.COMPANY_ADMIN, now=datetime.now(UTC) - timedelta(hours=1))}"
    headers = {} if authorization_header is None else {"Authorization": authorization_header}

    response = client.post(_path(Permission.POLICY_READ), headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": detail}
    assert reached == []


@pytest.mark.security
@pytest.mark.parametrize(
    "placement",
    ["query-access_token", "query-token", "cookie-access_token", "cookie-authorization", "header-x-access-token",
     "header-x-authorization", "authorization-without-scheme", "authorization-basic-scheme"],
)
def test_a_valid_admin_token_anywhere_but_a_bearer_authorization_header_is_401(
    client: TestClient, token_settings: Settings, monkeypatch: pytest.MonkeyPatch, placement: str
) -> None:
    # Phase 1.9: there is one authentication path. A genuine token presented any other way authenticates nothing.
    reached: list[str] = []
    monkeypatch.setattr(authorization, "require_permission", lambda *a: reached.append("rbac"))
    monkeypatch.setattr(tenancy_api, "resolve_tenant", lambda *a: reached.append("tenant"))
    token = _token(token_settings, UserRole.COMPANY_ADMIN)
    request_kwargs = {
        "query-access_token": {"params": {"access_token": token}},
        "query-token": {"params": {"token": token}},
        "cookie-access_token": {"headers": {"Cookie": f"access_token={token}"}},
        "cookie-authorization": {"headers": {"Cookie": f"Authorization=Bearer {token}"}},
        "header-x-access-token": {"headers": {"X-Access-Token": token}},
        "header-x-authorization": {"headers": {"X-Authorization": f"Bearer {token}"}},
        "authorization-without-scheme": {"headers": {"Authorization": token}},
        "authorization-basic-scheme": {"headers": {"Authorization": f"Basic {token}"}},
    }[placement]

    response = client.post(_path(Permission.COMPANY_USER_MANAGE), **request_kwargs)

    assert response.status_code == 401
    assert response.json() == {"detail": NOT_AUTHENTICATED_MESSAGE}
    assert reached == []


# --- Unknown roles and permissions fail closed ----------------------------------------------------------------


@pytest.mark.security
def test_token_with_an_unknown_role_is_rejected_at_authentication(client: TestClient, jwt_keys: RsaKeyPair) -> None:
    # A validly signed token naming a role that does not exist never becomes an identity: 401, not even 403.
    now = int(datetime.now(UTC).timestamp())
    claims = {"sub": str(uuid.uuid4()), "company_id": str(COMPANY_A), "role": "SUPER_ADMIN", "iat": now,
              "exp": now + 900, "jti": str(uuid.uuid4())}

    response = client.post(_path(Permission.POLICY_READ), headers=_bearer(jwt.encode(claims, jwt_keys.private_pem,
                                                                                    algorithm=ALGORITHM)))

    assert response.status_code == 401


@pytest.mark.security
def test_authenticated_role_missing_from_the_mapping_is_403_everywhere(
    client: TestClient, token_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A recognised role that the mapping does not list (e.g. one added to UserRole but not mapped) gets nothing.
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType({UserRole.COMPANY_ADMIN: frozenset(Permission)}))
    token = _token(token_settings, UserRole.STAFF)

    statuses = {client.post(_path(permission), headers=_bearer(token)).status_code for permission in Permission}

    assert statuses == {403}


@pytest.mark.security
def test_permission_granted_to_no_role_is_403_for_everyone(
    client: TestClient, token_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    reduced = {role: granted - {Permission.POLICY_MANAGE} for role, granted in ROLE_PERMISSIONS.items()}
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType(reduced))

    for role in UserRole:
        response = client.post(_path(Permission.POLICY_MANAGE), headers=_bearer(_token(token_settings, role)))
        assert response.status_code == 403


# --- Client-supplied role and company -------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"params": {"role": "COMPANY_ADMIN"}},
        {"headers": {"X-Role": "COMPANY_ADMIN"}},
        {"headers": {"Cookie": "role=COMPANY_ADMIN"}},
        {"json": {"role": "COMPANY_ADMIN", "permissions": [p.value for p in Permission]}},
        {"params": {"role": "COMPANY_ADMIN"}, "headers": {"X-Role": "COMPANY_ADMIN", "Cookie": "role=COMPANY_ADMIN"},
         "json": {"role": "COMPANY_ADMIN"}},
    ],
    ids=["query", "header", "cookie", "body", "all"],
)
def test_client_role_cannot_elevate_staff(client: TestClient, token_settings: Settings, request_kwargs: dict[str, Any]) -> None:
    kwargs = dict(request_kwargs)
    headers = kwargs.pop("headers", {}) | _bearer(_token(token_settings, UserRole.STAFF))

    denied = [client.post(_path(p), headers=headers, **kwargs).status_code for p in ADMIN_ONLY]
    allowed = client.post(_path(Permission.POLICY_READ), headers=headers, **kwargs)

    assert denied == [403] * len(ADMIN_ONLY)
    assert allowed.json()["role"] == "STAFF"  # the identity's role, not the client's


@pytest.mark.security
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"params": {"permissions": [p.value for p in Permission], "permission": "company:user:manage"}},
        {"headers": {"X-Permissions": ",".join(Permission), "X-Permission": "company:user:manage"}},
        {"headers": {"Cookie": "permissions=company:user:manage; permission=company:user:manage"}},
        {"json": {"permissions": [p.value for p in Permission], "permission": "company:user:manage"}},
    ],
    ids=["query", "header", "cookie", "body"],
)
def test_client_permissions_cannot_grant_staff_an_admin_permission(
    client: TestClient, token_settings: Settings, request_kwargs: dict[str, Any]
) -> None:
    # Phase 1.9: permissions come only from ROLE_PERMISSIONS for the verified role, whatever the client claims.
    kwargs = dict(request_kwargs)
    headers = kwargs.pop("headers", {}) | _bearer(_token(token_settings, UserRole.STAFF))

    denied = [client.post(_path(p), headers=headers, **kwargs).status_code for p in ADMIN_ONLY]

    assert denied == [403] * len(ADMIN_ONLY)


@pytest.mark.security
@pytest.mark.parametrize("role", list(UserRole))
def test_client_company_id_changes_neither_the_decision_nor_the_tenant(
    client: TestClient, token_settings: Settings, role: UserRole
) -> None:
    claimed = str(COMPANY_B)
    headers = _bearer(_token(token_settings, role, COMPANY_A)) | {"X-Company-Id": claimed,
                                                                  "Cookie": f"company_id={claimed}"}

    for permission in Permission:
        response = client.post(_path(permission), headers=headers, params={"company_id": claimed},
                               json={"company_id": claimed})
        assert response.status_code == (200 if permission in ROLE_PERMISSIONS[role] else 403)
        if response.status_code == 200:
            assert response.json()["tenant"] == str(COMPANY_A)


@pytest.mark.security
def test_company_admin_stays_in_its_own_company(client: TestClient, token_settings: Settings) -> None:
    for company in (COMPANY_A, COMPANY_B):
        response = client.post(_path(Permission.COMPANY_USER_MANAGE),
                               headers=_bearer(_token(token_settings, UserRole.COMPANY_ADMIN, company)),
                               params={"company_id": str(COMPANY_B if company == COMPANY_A else COMPANY_A)})
        assert response.json()["tenant"] == str(company)


# --- One identity source --------------------------------------------------------------------------------------


@pytest.mark.security
def test_role_and_company_come_only_from_the_authenticated_identity(protected_app: FastAPI) -> None:
    # With current_identity replaced, both the permission decision and the tenant follow it, with no Authorization
    # header at all: neither the permission check nor tenant resolution decodes a token of its own.
    admin_b = TokenIdentity(user_id=uuid.uuid4(), company_id=COMPANY_B, role=UserRole.COMPANY_ADMIN)
    protected_app.dependency_overrides[current_identity] = lambda: admin_b

    with TestClient(protected_app) as client:
        response = client.post(_path(Permission.POLICY_MANAGE), params={"role": "STAFF", "company_id": str(COMPANY_A)})

    assert response.status_code == 200
    assert response.json() == {"user_id": str(admin_b.user_id), "role": "COMPANY_ADMIN", "tenant": str(COMPANY_B)}


@pytest.mark.security
def test_token_is_decoded_once_per_protected_request(
    client: TestClient, token_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_decode = authorization.decode_access_token
    monkeypatch.setattr(authorization, "decode_access_token", lambda t, s: calls.append(t) or real_decode(t, s))

    response = client.post(_path(Permission.POLICY_READ), headers=_bearer(_token(token_settings)))

    assert response.status_code == 200
    assert len(calls) == 1


# --- Service-level checks and errors --------------------------------------------------------------------------


@pytest.mark.security
def test_permission_denial_raised_inside_a_route_is_a_generic_403(
    client: TestClient, token_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    denied = client.post("/_service_check_test", headers=_bearer(_token(token_settings, UserRole.STAFF)))
    allowed = client.post("/_service_check_test", headers=_bearer(_token(token_settings, UserRole.COMPANY_ADMIN)))
    logs = capsys.readouterr().err

    assert denied.status_code == 403
    assert denied.json() == {"detail": FORBIDDEN_MESSAGE}
    assert allowed.json() == {"ok": True}
    assert "Traceback" not in logs and "status=500" not in logs  # an expected denial, not an unexpected error
    assert "authorization denied user_id=" in logs and "status=403" in logs


@pytest.mark.security
def test_401_and_403_reveal_nothing_and_tokens_are_not_logged(
    client: TestClient, token_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    staff_token = _token(token_settings, UserRole.STAFF)

    responses = [
        client.post(_path(Permission.POLICY_MANAGE)),
        client.post(_path(Permission.POLICY_MANAGE), headers=_bearer("a.b.c")),
        client.post(_path(Permission.POLICY_MANAGE), headers=_bearer(staff_token)),
    ]
    logs = capsys.readouterr().err

    assert [response.status_code for response in responses] == [401, 401, 403]
    for response in responses:
        assert set(response.json()) == {"detail"}
        for detail in ("policy", "STAFF", "ROLE_PERMISSIONS", "Traceback", "rbac"):
            assert detail not in response.text
    for secret in (staff_token, *staff_token.split(".")[1:]):
        assert secret not in logs


# --- Every route must declare its permission ------------------------------------------------------------------


def test_real_application_routes_are_public_or_declare_a_permission(settings: Settings) -> None:
    app = create_app(settings)

    assert routes_missing_permission(app) == []
    assert PUBLIC_PATHS == {"/", "/health"}
    # Sign-in (D-032), company registration (D-035) and the demo session (D-036) are the public writes;
    # tests/api/test_route_security.py pins them.
    assert PUBLIC_WRITES == {("/auth/login", "POST"), ("/auth/register-company", "POST"),
                             ("/auth/demo-session", "POST")}
    # Phase 2.4 added the policy and document API, Phase 6 search, Phase 8 chat, and D-035 the PDF upload; none of
    # it is public.
    assert set(app.openapi()["paths"]) == PUBLIC_PATHS | {"/auth/login", "/auth/register-company",
                                                          "/auth/demo-session"} | {
        "/policies", "/policies/{policy_id}", "/policies/{policy_id}/documents", "/documents/{document_id}",
        "/documents/{document_id}/status", "/search", "/chat", "/policies/{policy_id}/documents/upload"}


@pytest.mark.security
def test_create_app_refuses_to_start_with_a_route_that_declares_no_permission(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "healthy"}

    @router.get("/forgotten-permission")
    def forgotten() -> dict[str, str]:
        return {"secret": "data"}

    monkeypatch.setattr(main, "health_router", router)

    with pytest.raises(RuntimeError, match=r"GET /forgotten-permission"):
        create_app(settings)


def test_permission_declarations_are_found_wherever_they_are_made() -> None:
    app = FastAPI()
    check = permission_required(Permission.POLICY_READ)
    router_level = APIRouter(dependencies=[Depends(check)])
    include_level, plain = APIRouter(), APIRouter()

    @router_level.get("/router-level")
    def a() -> None: ...

    @include_level.get("/include-level")
    def b() -> None: ...

    @plain.get("/in-plain-router")
    def c() -> None: ...

    @app.get("/route-level", dependencies=[Depends(check)])
    def d() -> None: ...

    @app.get("/parameter-level")
    def e(identity: Annotated[TokenIdentity, Depends(permission_required(Permission.CHAT_USE))]) -> None: ...

    @app.get("/authenticated-only")
    def f(identity: Annotated[TokenIdentity, Depends(current_identity)]) -> None: ...

    @app.get("/tenant-only")
    def g(tenant: Annotated[TenantIdentity, Depends(current_tenant)]) -> None: ...

    app.include_router(router_level)
    app.include_router(include_level, prefix="/x", dependencies=[Depends(check)])
    app.include_router(plain)

    # Authentication or a tenant alone is not a permission declaration.
    assert sorted(routes_missing_permission(app)) == ["GET /authenticated-only", "GET /in-plain-router",
                                                      "GET /tenant-only"]
