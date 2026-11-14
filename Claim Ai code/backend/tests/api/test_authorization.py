"""API tests for the HTTP authorization boundary (app/api/authorization.py).

401 = missing or invalid authentication; 403 = a valid identity whose role lacks the permission.

The protected routes here are test-only. They are added to a test app instance in this module, never to the real
application, which still has no protected routes.
"""

import base64
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import pytest
from fastapi import Body, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.authorization import NOT_AUTHENTICATED_MESSAGE, permission_required
from app.auth import AuthenticatedUser
from app.config import Settings
from app.errors import INTERNAL_ERROR_DETAIL
from app.main import create_app
from app.models import UserRole, UserStatus
from app.rbac import FORBIDDEN_MESSAGE, Permission
from app.tokens import GENERIC_TOKEN_FAILURE_MESSAGE, TokenIdentity, issue_access_token
from tests.conftest import RsaKeyPair, forge_claims, generate_rsa_key_pair
from tests.unit.test_rbac import CASE_IDS, CASES

ADMIN, STAFF = UserRole.COMPANY_ADMIN, UserRole.STAFF
ADMIN_ONLY = [permission for permission in Permission if permission not in {Permission.POLICY_READ,
                                                                           Permission.SEARCH_USE,
                                                                           Permission.CHAT_USE}]


def _path(permission: Permission) -> str:
    return f"/_rbac_test/{permission.name.lower()}"


def _add_test_route(app: FastAPI, permission: Permission) -> None:
    @app.post(_path(permission))
    def protected(
        identity: Annotated[TokenIdentity, Depends(permission_required(permission))],
        payload: Annotated[dict[str, Any] | None, Body()] = None,
        role: str | None = None,
    ) -> dict[str, str]:
        # Accepts a body and a "role" query parameter only to show that neither influences authorization.
        return {"user_id": str(identity.user_id), "company_id": str(identity.company_id), "role": identity.role.value}


def _protected_app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    for permission in Permission:
        _add_test_route(app, permission)
    return app


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def protected_client(token_settings: Settings) -> Iterator[TestClient]:
    with TestClient(_protected_app(token_settings)) as client:
        yield client


def _user(role: UserRole) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uuid.uuid4(), company_id=uuid.uuid4(), email=f"{role.value.lower()}@acme.example",
                             full_name="Test User", role=role, status=UserStatus.ACTIVE)


def _token(role: UserRole, settings: Settings, **kwargs: Any) -> str:
    return issue_access_token(_user(role), settings, **kwargs)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _settings_with_another_key_pair() -> Settings:
    """An unrelated signer, e.g. an attacker's own key pair."""
    other = generate_rsa_key_pair()
    return Settings(_env_file=None, jwt_private_key=other.private_pem, jwt_public_key=other.public_pem)


# --- 401: missing or invalid authentication -------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "authorization",
    [None, "", "Bearer", "Bearer ", "Basic dXNlcjpmYWtlLXBhc3N3b3Jk", "Token abc"],
    ids=["no-header", "empty-header", "scheme-only", "empty-token", "basic-scheme", "other-scheme"],
)
def test_missing_authentication_is_401(protected_client: TestClient, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}

    response = protected_client.post(_path(Permission.POLICY_READ), headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": NOT_AUTHENTICATED_MESSAGE}
    assert response.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.security
@pytest.mark.parametrize(
    "make_token",
    [
        lambda s: "not-a-jwt",
        lambda s: "a b",
        lambda s: _token(ADMIN, s, now=datetime.now(UTC) - timedelta(hours=1)),
        lambda s: forge_claims(_token(STAFF, s), role="COMPANY_ADMIN"),
        lambda s: f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': str(uuid.uuid4()), 'role': 'COMPANY_ADMIN'})}.",
        lambda s: _token(ADMIN, _settings_with_another_key_pair()),
    ],
    ids=["garbage", "two-words", "expired-admin", "staff-forged-as-admin", "unsigned-admin", "admin-from-another-key"],
)
def test_invalid_authentication_is_401_not_403(
    protected_client: TestClient, token_settings: Settings, make_token: Any
) -> None:
    response = protected_client.post(_path(Permission.POLICY_MANAGE), headers=_bearer(make_token(token_settings)))

    assert response.status_code == 401
    assert response.json() == {"detail": GENERIC_TOKEN_FAILURE_MESSAGE}
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


# --- 403 and allow: valid authentication ----------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("permission", ADMIN_ONLY, ids=[p.value for p in ADMIN_ONLY])
def test_valid_staff_identity_without_the_permission_is_403(
    protected_client: TestClient, token_settings: Settings, permission: Permission
) -> None:
    response = protected_client.post(_path(permission), headers=_bearer(_token(STAFF, token_settings)))

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_MESSAGE}
    assert "WWW-Authenticate" not in response.headers


@pytest.mark.security
@pytest.mark.parametrize(("role", "permission", "allowed"), CASES, ids=CASE_IDS)
def test_http_permission_matrix(
    protected_client: TestClient, token_settings: Settings, role: UserRole, permission: str, allowed: bool
) -> None:
    user = _user(role)

    response = protected_client.post(_path(Permission(permission)), headers=_bearer(issue_access_token(user, token_settings)))

    if allowed:
        assert response.status_code == 200
        assert response.json() == {"user_id": str(user.user_id), "company_id": str(user.company_id), "role": role.value}
    else:
        assert response.status_code == 403
        assert response.json() == {"detail": FORBIDDEN_MESSAGE}


@pytest.mark.security
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {"role": "COMPANY_ADMIN", "permissions": [p.value for p in Permission],
                  "company_id": str(uuid.uuid4())}},
        {"params": {"role": "COMPANY_ADMIN", "permissions": "policy:manage", "company_id": str(uuid.uuid4())}},
        {"headers": {"X-Role": "COMPANY_ADMIN", "X-Permissions": "policy:manage", "X-Company-Id": str(uuid.uuid4())}},
        {"json": {"role": "COMPANY_ADMIN"}, "params": {"role": "COMPANY_ADMIN"}, "headers": {"X-Role": "COMPANY_ADMIN"}},
    ],
    ids=["request-body", "query-string", "extra-headers", "all-at-once"],
)
def test_client_supplied_role_cannot_escalate_staff(
    protected_client: TestClient, token_settings: Settings, request_kwargs: dict[str, Any]
) -> None:
    kwargs = dict(request_kwargs)  # parametrize values are shared: never mutate them
    headers = kwargs.pop("headers", {}) | _bearer(_token(STAFF, token_settings))

    response = protected_client.post(_path(Permission.POLICY_MANAGE), headers=headers, **kwargs)

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_MESSAGE}


# --- What is exposed ------------------------------------------------------------------------------------------


@pytest.mark.security
def test_denials_expose_nothing_in_responses_or_logs(
    token_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    staff_token = _token(STAFF, token_settings)
    forged = forge_claims(staff_token, role="COMPANY_ADMIN")

    with TestClient(_protected_app(token_settings)) as client:
        responses = [
            client.post(_path(Permission.POLICY_MANAGE)),
            client.post(_path(Permission.POLICY_MANAGE), headers=_bearer(forged)),
            client.post(_path(Permission.POLICY_MANAGE), headers=_bearer(staff_token)),
        ]
    logs = capsys.readouterr().err

    assert [response.status_code for response in responses] == [401, 401, 403]
    for response in responses:
        assert set(response.json()) == {"detail"}
        for detail in ("policy", "manage", "STAFF", "COMPANY_ADMIN", "jwt", "Traceback"):
            assert detail not in response.text
    assert "status=401" in logs and "status=403" in logs
    assert "authorization denied user_id=" in logs and "permission=policy:manage" in logs
    for secret in (staff_token, forged, *staff_token.split(".")[1:], *forged.split(".")[1:]):
        assert secret not in logs


@pytest.mark.security
def test_without_configured_keys_a_bearer_token_fails_closed_with_a_generic_500(settings: Settings) -> None:
    with TestClient(_protected_app(settings)) as client:  # default settings: no JWT keys
        with_token = client.post(_path(Permission.POLICY_READ), headers=_bearer("any-token"))
        without_token = client.post(_path(Permission.POLICY_READ))

    assert with_token.status_code == 500
    assert with_token.json() == {"detail": INTERNAL_ERROR_DETAIL}
    assert without_token.status_code == 401


def test_permission_required_rejects_a_non_permission_when_the_route_is_defined() -> None:
    with pytest.raises(TypeError, match="Permission member"):
        permission_required("policy:read")  # type: ignore[arg-type]


def test_real_application_protected_routes_are_exactly_the_policy_search_and_chat_api(settings: Settings) -> None:
    # Until Phase 2.3 the application had no protected routes. Phase 2.4 added the policy and document API, Phase 6
    # the search route and Phase 8 the chat route, and nothing else: every path and method is listed. Sign-in
    # (POST /auth/login, D-032), company registration (POST /auth/register-company, D-035) and the demo session
    # (POST /auth/demo-session, D-036) are the public writes; the PDF upload (D-035) is protected like the other
    # document changes.
    paths = create_app(settings).openapi()["paths"]

    assert {path: sorted(methods) for path, methods in paths.items()} == {
        "/": ["get"],
        "/health": ["get"],
        "/auth/login": ["post"],
        "/auth/register-company": ["post"],
        "/auth/demo-session": ["post"],
        "/policies": ["get", "post"],
        "/policies/{policy_id}": ["delete", "get", "patch"],
        "/policies/{policy_id}/documents": ["get", "post"],
        "/policies/{policy_id}/documents/upload": ["post"],
        "/documents/{document_id}": ["delete", "get", "patch"],
        "/documents/{document_id}/status": ["put"],
        "/search": ["post"],
        "/chat": ["post"],
    }
