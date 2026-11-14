"""API tests for the tenant-resolution dependency (app/api/tenancy.py).

The tenant is the verified token's company id. A company id sent in the path, query string, body, a header or a
cookie reaches the route, but never changes the resolved tenant.

The routes here are test-only: they are added to a test app instance, never to the real application.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
import pytest
from fastapi import Body, Cookie, Depends, FastAPI, Header
from fastapi.testclient import TestClient

import app.api.authorization as authorization
from app.api.authorization import NOT_AUTHENTICATED_MESSAGE, current_identity, permission_required
from app.api.tenancy import current_tenant
from app.auth import AuthenticatedUser
from app.config import Settings
from app.main import create_app
from app.models import UserRole, UserStatus
from app.rbac import FORBIDDEN_MESSAGE, Permission
from app.tenancy import TenantIdentity
from app.tokens import ALGORITHM, GENERIC_TOKEN_FAILURE_MESSAGE, TokenIdentity, issue_access_token
from tests.conftest import RsaKeyPair, forge_claims, generate_rsa_key_pair

COMPANY_A, COMPANY_B = uuid.uuid4(), uuid.uuid4()
UNUSED_PATH = "unused"
TRANSPORTS = ["query", "path", "body", "header", "cookie"]


def _tenant_app(settings: Settings) -> FastAPI:
    app = create_app(settings)

    @app.post("/_tenant_test/{path_company_id}")
    def echo_tenant(
        tenant: Annotated[TenantIdentity, Depends(current_tenant)],
        path_company_id: str,
        company_id: str | None = None,
        x_company_id: Annotated[str | None, Header()] = None,
        cookie_company_id: Annotated[str | None, Cookie(alias="company_id")] = None,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        # The client's values are echoed only to prove they reached the route; the tenant ignores all of them.
        received = {
            "path": None if path_company_id == UNUSED_PATH else path_company_id,
            "query": company_id,
            "header": x_company_id,
            "cookie": cookie_company_id,
            "body": (payload or {}).get("company_id"),
        }
        return {"tenant": str(tenant.company_id), "received": received}

    for permission in (Permission.POLICY_READ, Permission.POLICY_MANAGE):

        @app.post(f"/_tenant_rbac_test/{permission.name.lower()}")
        def tenant_and_permission(
            identity: Annotated[TokenIdentity, Depends(permission_required(permission))],
            tenant: Annotated[TenantIdentity, Depends(current_tenant)],
        ) -> dict[str, str]:
            return {"tenant": str(tenant.company_id), "role": identity.role.value}

    return app


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def tenant_app(token_settings: Settings) -> FastAPI:
    return _tenant_app(token_settings)


@pytest.fixture
def tenant_client(tenant_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(tenant_app) as client:
        yield client


def _user(company_id: uuid.UUID, role: UserRole = UserRole.STAFF) -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uuid.uuid4(), company_id=company_id, email="user@acme.example",
                             full_name="Test User", role=role, status=UserStatus.ACTIVE)


def _token(settings: Settings, company_id: uuid.UUID = COMPANY_A, role: UserRole = UserRole.STAFF, **kwargs: Any) -> str:
    return issue_access_token(_user(company_id, role), settings, **kwargs)


def _signed(claims: dict[str, Any], jwt_keys: RsaKeyPair) -> str:
    """Sign arbitrary claims with the real test key: a token the service itself would never issue."""
    now = int(datetime.now(UTC).timestamp())
    base = {"sub": str(uuid.uuid4()), "company_id": str(COMPANY_A), "role": "STAFF", "iat": now, "exp": now + 900,
            "jti": str(uuid.uuid4())}
    return jwt.encode(base | claims, jwt_keys.private_pem, algorithm=ALGORITHM)


def _send(client: TestClient, token: str | None, claimed: dict[str, str]) -> Any:
    """POST with the given client-claimed company ids, one per transport."""
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    if "header" in claimed:
        headers["X-Company-Id"] = claimed["header"]
    if "cookie" in claimed:
        headers["Cookie"] = f"company_id={claimed['cookie']}"
    return client.post(
        f"/_tenant_test/{claimed.get('path', UNUSED_PATH)}",
        headers=headers,
        params={"company_id": claimed["query"]} if "query" in claimed else None,
        json={"company_id": claimed["body"]} if "body" in claimed else None,
    )


# --- Valid authentication -------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_valid_token_resolves_its_own_company(tenant_client: TestClient, token_settings: Settings, role: UserRole) -> None:
    response = _send(tenant_client, _token(token_settings, COMPANY_A, role), {})

    assert response.status_code == 200
    assert response.json()["tenant"] == str(COMPANY_A)


@pytest.mark.security
def test_colleagues_share_a_tenant_and_other_companies_do_not(
    tenant_client: TestClient, token_settings: Settings
) -> None:
    tenants = [
        _send(tenant_client, _token(token_settings, company, role), {}).json()["tenant"]
        for company, role in ((COMPANY_A, UserRole.STAFF), (COMPANY_A, UserRole.COMPANY_ADMIN), (COMPANY_B, UserRole.STAFF))
    ]

    assert tenants == [str(COMPANY_A), str(COMPANY_A), str(COMPANY_B)]


# --- Missing or invalid authentication ------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("authorization_header", [None, "", "Bearer ", "Basic dXNlcjpmYWtl"],
                         ids=["no-header", "empty", "empty-token", "basic"])
def test_missing_authentication_is_401(tenant_client: TestClient, authorization_header: str | None) -> None:
    headers = {} if authorization_header is None else {"Authorization": authorization_header}

    response = tenant_client.post(f"/_tenant_test/{UNUSED_PATH}", headers=headers, params={"company_id": str(COMPANY_B)})

    assert response.status_code == 401
    assert response.json() == {"detail": NOT_AUTHENTICATED_MESSAGE}


@pytest.mark.security
@pytest.mark.parametrize(
    "make_token",
    [
        lambda s, keys: "not-a-jwt",
        lambda s, keys: _token(s, COMPANY_A, now=datetime.now(UTC) - timedelta(hours=1)),
        lambda s, keys: forge_claims(_token(s, COMPANY_A), company_id=str(COMPANY_B)),
        lambda s, keys: _token(_other_signer(), COMPANY_B),
        lambda s, keys: f"{_unsigned_header()}.{forge_claims(_token(s, COMPANY_A), company_id=str(COMPANY_B)).split('.')[1]}.",
        lambda s, keys: _signed({"company_id": "not-a-uuid"}, keys),
        lambda s, keys: _signed({"company_id": str(uuid.UUID(int=0))}, keys),
    ],
    ids=["garbage", "expired", "forged-company", "wrong-key", "alg-none", "signed-malformed-company",
         "signed-nil-company"],
)
def test_invalid_forged_or_tenantless_token_is_401(
    tenant_client: TestClient, token_settings: Settings, jwt_keys: RsaKeyPair, make_token: Any
) -> None:
    response = _send(tenant_client, make_token(token_settings, jwt_keys), {})

    assert response.status_code == 401
    assert response.json() == {"detail": GENERIC_TOKEN_FAILURE_MESSAGE}
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'


def _other_signer() -> Settings:
    other = generate_rsa_key_pair()
    return Settings(_env_file=None, jwt_private_key=other.private_pem, jwt_public_key=other.public_pem)


def _unsigned_header() -> str:
    return "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0"  # base64url of {"alg":"none","typ":"JWT"}


# --- Client-supplied company ids ------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_client_company_id_cannot_select_the_tenant(
    tenant_client: TestClient, token_settings: Settings, transport: str
) -> None:
    response = _send(tenant_client, _token(token_settings, COMPANY_A), {transport: str(COMPANY_B)})
    body = response.json()

    assert response.status_code == 200
    assert body["received"][transport] == str(COMPANY_B)  # the route did receive the client's value ...
    assert body["tenant"] == str(COMPANY_A)  # ... and the tenant is still the token's company


@pytest.mark.security
@pytest.mark.parametrize(
    ("own", "claimed"), [(COMPANY_A, COMPANY_B), (COMPANY_B, COMPANY_A)], ids=["user-a-claims-b", "user-b-claims-a"]
)
def test_cross_tenant_boundary_holds_in_both_directions(
    tenant_client: TestClient, token_settings: Settings, own: uuid.UUID, claimed: uuid.UUID
) -> None:
    every_transport = dict.fromkeys(TRANSPORTS, str(claimed))

    response = _send(tenant_client, _token(token_settings, own), every_transport)

    assert response.status_code == 200
    assert response.json() == {"tenant": str(own), "received": every_transport}


@pytest.mark.security
@pytest.mark.parametrize("claimed", ["", "not-a-uuid", str(uuid.UUID(int=0)), "../admin"],
                         ids=["empty", "malformed", "nil", "path-like"])
def test_junk_client_company_ids_are_ignored_not_used_as_a_fallback(
    tenant_client: TestClient, token_settings: Settings, claimed: str
) -> None:
    response = _send(tenant_client, _token(token_settings, COMPANY_A), {"query": claimed, "header": claimed,
                                                                         "body": claimed})

    assert response.status_code == 200
    assert response.json()["tenant"] == str(COMPANY_A)


# --- One authentication path ----------------------------------------------------------------------------------


@pytest.mark.security
def test_tenant_comes_only_through_current_identity(tenant_app: FastAPI, token_settings: Settings) -> None:
    # With current_identity replaced, the tenant follows it, even with no Authorization header or a token for
    # another company: current_tenant neither reads headers nor decodes tokens itself.
    tenant_app.dependency_overrides[current_identity] = lambda: TokenIdentity(
        user_id=uuid.uuid4(), company_id=COMPANY_A, role=UserRole.STAFF
    )
    with TestClient(tenant_app) as client:
        without_header = _send(client, None, {})
        with_other_token = _send(client, _token(token_settings, COMPANY_B), {})

    assert without_header.json()["tenant"] == str(COMPANY_A)
    assert with_other_token.json()["tenant"] == str(COMPANY_A)


@pytest.mark.security
def test_token_is_verified_once_and_rbac_is_unchanged_alongside_the_tenant(
    tenant_client: TestClient, token_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_decode = authorization.decode_access_token

    def counting_decode(token: str, settings: Settings) -> TokenIdentity:
        calls.append(token)
        return real_decode(token, settings)

    monkeypatch.setattr(authorization, "decode_access_token", counting_decode)
    headers = {"Authorization": f"Bearer {_token(token_settings, COMPANY_B)}"}

    allowed = tenant_client.post("/_tenant_rbac_test/policy_read", headers=headers)
    decodes_per_request = len(calls)
    denied = tenant_client.post("/_tenant_rbac_test/policy_manage", headers=headers)

    assert decodes_per_request == 1
    assert allowed.status_code == 200
    assert allowed.json() == {"tenant": str(COMPANY_B), "role": "STAFF"}
    assert denied.status_code == 403
    assert denied.json() == {"detail": FORBIDDEN_MESSAGE}


# --- What is exposed ------------------------------------------------------------------------------------------


@pytest.mark.security
def test_logs_contain_no_token_or_client_company_ids(
    token_settings: Settings, jwt_keys: RsaKeyPair, capsys: pytest.CaptureFixture[str]
) -> None:
    token = _token(token_settings, COMPANY_A)
    nil_token = _signed({"company_id": str(uuid.UUID(int=0))}, jwt_keys)
    # The request log records the URL path by design (Phase 0.6), so a value placed in the path appears there as
    # part of the path. The query string, body, headers and cookies are never logged.
    not_in_path = {transport: str(COMPANY_B) for transport in TRANSPORTS if transport != "path"}

    with TestClient(_tenant_app(token_settings)) as client:
        _send(client, token, not_in_path)
        _send(client, nil_token, {})
    logs = capsys.readouterr().err

    assert "status=200" in logs and "status=401" in logs
    assert "tenant resolution failed: no valid company_id" in logs
    for value in (token, nil_token, *token.split(".")[1:], *nil_token.split(".")[1:], str(COMPANY_B)):
        assert value not in logs
