"""Phase 1.8 — cross-tenant security verification of the whole chain, against PostgreSQL.

    client request → JWT authentication (current_identity) → TokenIdentity → RBAC (permission_required)
        → tenant resolution (current_tenant) → TenantDatabase → tenant-owned rows

Nothing in the chain is mocked. The routes are test-only (added to a test app instance) and use the real
dependencies and the real TenantDatabase on the throwaway test database. ``executed`` records the tenant of every
query TenantDatabase actually runs, so "the database was never queried" and "the query was scoped to A" are observed
directly.

World: company A {admin@a, staff@a} and company B {admin@b, staff@b}, all with the same fake password.
Each test case is labelled with its letter from the Phase 1.8 adversarial matrix (A–V).
"""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Annotated, Any

import jwt
import psycopg
import pytest
from fastapi import APIRouter, Body, Cookie, Depends, FastAPI, Header
from fastapi.testclient import TestClient

import app.rbac as rbac
from app.api.authorization import permission_required
from app.api.tenancy import current_tenant
from app.auth import AuthenticatedUser, authenticate
from app.config import Settings
from app.main import create_app
from app.models import UserRole, UserStatus
from app.passwords import hash_password
from app.rbac import FORBIDDEN_MESSAGE, ROLE_PERMISSIONS, Permission
from app.tenancy import TenantIdentity
from app.tenant_db import TenantDatabase
from app.tokens import ALGORITHM, issue_access_token
from tests.conftest import RsaKeyPair, forge_claims, generate_rsa_key_pair

pytestmark = [pytest.mark.database, pytest.mark.security]

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]

PASSWORD = "fake-Passw0rd-for-cross-tenant-tests"
EMAILS = {"A": ["admin@a.example", "staff@a.example"], "B": ["admin@b.example", "staff@b.example"]}
OTHER = {"A": "B", "B": "A"}
TRANSPORTS = ["path", "query", "body", "header", "cookie", "all"]


# --- World, app and helpers -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def world(insert_company: Insert, insert_user: Insert, password_hash: str) -> dict[str, Row]:
    rows: dict[str, Row] = {"A": insert_company(name="Company A", slug="xt-a"),
                            "B": insert_company(name="Company B", slug="xt-b")}
    for company in ("A", "B"):
        for role, prefix in (("COMPANY_ADMIN", "admin"), ("STAFF", "staff")):
            rows[f"{prefix}_{company.lower()}"] = insert_user(
                company_id=rows[company]["id"], email=f"{prefix}@{company.lower()}.example", role=role,
                password_hash=password_hash, full_name=f"{prefix} {company}",
            )
    return rows


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def executed(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """The tenant (company id) of every query TenantDatabase executes during the test."""
    tenants: list[uuid.UUID] = []
    real_fetch = TenantDatabase._fetch

    def recording_fetch(self: TenantDatabase, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        tenants.append(self.tenant.company_id)
        return real_fetch(self, query, params)

    monkeypatch.setattr(TenantDatabase, "_fetch", recording_fetch)
    return tenants


def _client_claims(
    company_id: str | None = None,
    role: str | None = None,
    x_company_id: Annotated[str | None, Header()] = None,
    x_role: Annotated[str | None, Header()] = None,
    cookie_company_id: Annotated[str | None, Cookie(alias="company_id")] = None,
    cookie_role: Annotated[str | None, Cookie(alias="role")] = None,
    payload: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    """Every client-controlled tenant/role value the request carried. Echoed back only to prove the values arrived."""
    body = payload or {}
    return {"query": company_id, "query_role": role, "header": x_company_id, "header_role": x_role,
            "cookie": cookie_company_id, "cookie_role": cookie_role, "body": body.get("company_id"),
            "body_role": body.get("role")}


Claims = Annotated[dict[str, Any], Depends(_client_claims)]
Tenant = Annotated[TenantIdentity, Depends(current_tenant)]


def _app(settings: Settings, db: Conn) -> FastAPI:
    app = create_app(settings)
    xt = APIRouter(prefix="/_xt")

    def emails(tenant: TenantIdentity) -> list[str]:
        return sorted(user.email for user in TenantDatabase(db, tenant).list_users())

    @xt.post("/users", dependencies=[Depends(permission_required(Permission.COMPANY_USER_READ))])
    def company_users(tenant: Tenant, claims: Claims) -> dict[str, Any]:
        return {"emails": emails(tenant), "received": claims}

    @xt.post("/companies/{company_id}/users", dependencies=[Depends(permission_required(Permission.COMPANY_USER_READ))])
    def company_users_by_path(company_id: str, tenant: Tenant, claims: Claims) -> dict[str, Any]:
        return {"emails": emails(tenant), "received": claims | {"path": company_id}}

    @xt.post("/staff-read", dependencies=[Depends(permission_required(Permission.POLICY_READ))])
    def staff_read(tenant: Tenant, claims: Claims) -> dict[str, Any]:
        # A STAFF-permitted, tenant-scoped read. There are no policy tables yet, so it reads the tenant's users.
        return {"emails": emails(tenant), "received": claims}

    @xt.post("/companies/{company_id}/staff-read", dependencies=[Depends(permission_required(Permission.POLICY_READ))])
    def staff_read_by_path(company_id: str, tenant: Tenant, claims: Claims) -> dict[str, Any]:
        return {"emails": emails(tenant), "received": claims | {"path": company_id}}

    @xt.post("/manage-users", dependencies=[Depends(permission_required(Permission.COMPANY_USER_MANAGE))])
    def manage_users(tenant: Tenant, claims: Claims) -> dict[str, Any]:
        # An admin-only operation on tenant data (test-only; it changes nothing).
        return {"emails": emails(tenant), "received": claims}

    @xt.post("/companies/{company_id}/manage-users",
             dependencies=[Depends(permission_required(Permission.COMPANY_USER_MANAGE))])
    def manage_users_by_path(company_id: str, tenant: Tenant, claims: Claims) -> dict[str, Any]:
        return {"emails": emails(tenant), "received": claims | {"path": company_id}}

    @xt.post("/users/{user_id}", dependencies=[Depends(permission_required(Permission.COMPANY_USER_READ))])
    def one_user(user_id: uuid.UUID, tenant: Tenant) -> dict[str, Any]:
        user = TenantDatabase(db, tenant).get_user(user_id)
        return {"email": user.email if user else None}

    app.include_router(xt)
    return app


@pytest.fixture
def client(token_settings: Settings, db: Conn) -> Iterator[TestClient]:
    with TestClient(_app(token_settings, db)) as test_client:
        yield test_client


def _token(row: Row, settings: Settings, **kwargs: Any) -> str:
    """A token as issued after authenticate() for this user (see the whole-chain tests for the real login)."""
    user = AuthenticatedUser(user_id=row["id"], company_id=row["company_id"], email=row["email"],
                             full_name=row["full_name"], role=UserRole(row["role"]), status=UserStatus.ACTIVE)
    return issue_access_token(user, settings, **kwargs)


def _login(db: Conn, settings: Settings, company: str, email: str) -> str:
    """The real chain: credentials → authenticate() against the database → access token."""
    return issue_access_token(authenticate(db, f"xt-{company.lower()}", email, PASSWORD), settings)


def _send(client: TestClient, route: str, token: str | None, claimed: str | None = None, transport: str = "all",
          role: str | None = None, authorization: str | None = None) -> Any:
    """POST to /_xt/<route>, carrying the claimed company id (and role) by the given transport(s)."""
    used = TRANSPORTS[:-1] if transport == "all" else [transport]
    headers: dict[str, str] = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    elif token is not None:
        headers["Authorization"] = f"Bearer {token}"
    params, body, cookies = {}, {}, []
    if claimed is not None:
        if "query" in used:
            params["company_id"] = claimed
        if "body" in used:
            body["company_id"] = claimed
        if "header" in used:
            headers["X-Company-Id"] = claimed
        if "cookie" in used:
            cookies.append(f"company_id={claimed}")
    if role is not None:
        params["role"], body["role"], headers["X-Role"] = role, role, role
        cookies.append(f"role={role}")
    if cookies:
        headers["Cookie"] = "; ".join(cookies)
    path = f"/_xt/companies/{claimed}/{route}" if claimed is not None and "path" in used else f"/_xt/{route}"
    return client.post(path, headers=headers, params=params or None, json=body or None)


def _assert_no_data_leaked(response: Any) -> None:
    for company in ("A", "B"):
        for email in EMAILS[company]:
            assert email not in response.text


def _b64(data: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def _claims_for(row: Row, **changes: Any) -> dict[str, Any]:
    now = int(datetime.now(UTC).timestamp())
    return {"sub": str(row["id"]), "company_id": str(row["company_id"]), "role": row["role"], "iat": now,
            "exp": now + 900, "jti": str(uuid.uuid4())} | changes


# --- 5. Whole chain: real login → HTTP → RBAC → tenant → database ---------------------------------------------


@pytest.mark.parametrize("company", ["A", "B"])
def test_whole_chain_each_company_reads_only_its_own_records(
    client: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    company: str,
) -> None:
    token = _login(db, token_settings, company, f"admin@{company.lower()}.example")

    response = _send(client, "users", token)

    assert response.status_code == 200
    assert response.json()["emails"] == EMAILS[company]
    assert executed == [world[company]["id"]]
    for email in EMAILS[OTHER[company]]:
        assert email not in response.text


@pytest.mark.parametrize("company", ["A", "B"])
def test_whole_chain_with_the_other_company_claimed_everywhere_stays_in_its_own_company(
    client: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    company: str,
) -> None:
    token = _login(db, token_settings, company, f"admin@{company.lower()}.example")
    other_id = str(world[OTHER[company]]["id"])

    response = _send(client, "users", token, claimed=other_id, role="COMPANY_ADMIN")

    assert response.status_code == 200
    assert response.json()["emails"] == EMAILS[company]
    assert response.json()["received"]["header"] == other_id  # the claim did reach the application
    assert executed == [world[company]["id"]]
    for email in EMAILS[OTHER[company]]:
        assert email not in response.text


@pytest.mark.parametrize("company", ["A", "B"])
def test_whole_chain_staff_reads_own_company_and_is_denied_admin_operation_before_the_database(
    client: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    company: str,
) -> None:
    token = _login(db, token_settings, company, f"staff@{company.lower()}.example")

    allowed = _send(client, "staff-read", token)
    queries_after_allowed = list(executed)
    denied = _send(client, "manage-users", token, claimed=str(world[OTHER[company]]["id"]), role="COMPANY_ADMIN")

    assert allowed.json()["emails"] == EMAILS[company]
    assert queries_after_allowed == [world[company]["id"]]
    assert denied.status_code == 403
    assert denied.json() == {"detail": FORBIDDEN_MESSAGE}
    assert executed == queries_after_allowed  # the denied request ran no query


# --- 4. A–G, T: a client-controlled tenant never overrides the token's tenant ---------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
@pytest.mark.parametrize(("user", "route"), [("admin", "users"), ("staff", "staff-read")])
@pytest.mark.parametrize("company", ["A", "B"], ids=["A-claims-B", "B-claims-A"])
def test_client_tenant_in_any_transport_never_overrides_the_token_tenant(
    client: TestClient, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    transport: str, user: str, route: str, company: str,
) -> None:
    # A/B (both directions), C–G (admin via path/query/body/header/cookie), T (every transport at once).
    claimed = str(world[OTHER[company]]["id"])

    response = _send(client, route, _token(world[f"{user}_{company.lower()}"], token_settings), claimed, transport)
    body = response.json()

    assert response.status_code == 200
    assert body["emails"] == EMAILS[company]
    assert claimed in json.dumps(body["received"])  # the claimed company id reached the route ...
    assert executed == [world[company]["id"]]  # ... yet the only query ran for the token's company
    for email in EMAILS[OTHER[company]]:
        assert email not in response.text


# --- 4. H, I, V: STAFF with another company's id and admin claims is denied before the database ----------------


@pytest.mark.parametrize("route", ["manage-users", "users"])
@pytest.mark.parametrize("company", ["A", "B"], ids=["H-staff-A-claims-B", "I-staff-B-claims-A"])
def test_staff_attempting_an_admin_operation_is_denied_before_the_database(
    client: TestClient, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    route: str, company: str,
) -> None:
    response = _send(client, route, _token(world[f"staff_{company.lower()}"], token_settings),
                     claimed=str(world[OTHER[company]]["id"]), role="COMPANY_ADMIN")

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_MESSAGE}
    assert executed == []


# --- 4. J–P, Q, S: forged, foreign, unsigned, expired, missing, malformed or tenantless credentials ------------


def _other_key_token(world: dict[str, Row], settings: Settings, keys: RsaKeyPair) -> str:
    other = generate_rsa_key_pair()
    return jwt.encode(_claims_for(world["admin_b"]), other.private_pem, algorithm=ALGORITHM)


def _hs256_with_public_key(world: dict[str, Row], settings: Settings, keys: RsaKeyPair) -> str:
    unsigned = f"{_b64({'alg': 'HS256', 'typ': 'JWT'})}.{_b64(_claims_for(world['admin_b']))}"
    signature = hmac.new(keys.public_pem.encode(), unsigned.encode(), hashlib.sha256).digest()
    return f"{unsigned}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


ATTACKS: dict[str, Callable[[dict[str, Row], Settings, RsaKeyPair], str | None]] = {
    "J-forged-company": lambda w, s, k: forge_claims(_token(w["admin_a"], s), company_id=str(w["B"]["id"])),
    "K-forged-role": lambda w, s, k: forge_claims(_token(w["staff_a"], s), role="COMPANY_ADMIN"),
    "JK-forged-company-and-role": lambda w, s, k: forge_claims(_token(w["staff_a"], s), company_id=str(w["B"]["id"]),
                                                               role="COMPANY_ADMIN"),
    "L-wrong-key": _other_key_token,
    "M-alg-none": lambda w, s, k: f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(_claims_for(w['admin_b']))}.",
    "M-hs256-with-public-key": _hs256_with_public_key,
    "M-header-relabelled": lambda w, s, k: _b64({"alg": "HS256", "typ": "JWT"}) + "." + _token(w["admin_a"], s)
    .split(".", 1)[1],
    "N-expired": lambda w, s, k: _token(w["admin_a"], s, now=datetime.now(UTC) - timedelta(hours=1)),
    "Q-unknown-role-validly-signed": lambda w, s, k: jwt.encode(_claims_for(w["admin_a"], role="SUPER_ADMIN"),
                                                                k.private_pem, algorithm=ALGORITHM),
    "S-nil-tenant-validly-signed": lambda w, s, k: jwt.encode(_claims_for(w["admin_a"], company_id=str(uuid.UUID(int=0))),
                                                              k.private_pem, algorithm=ALGORITHM),
    "S-malformed-tenant-validly-signed": lambda w, s, k: jwt.encode(_claims_for(w["admin_a"], company_id="xt-b"),
                                                                    k.private_pem, algorithm=ALGORITHM),
}
MALFORMED_AUTHORIZATION = {
    "O-missing": None,
    "P-scheme-only": "Bearer",
    "P-other-scheme": "Basic YWRtaW5AYS5leGFtcGxlOmZha2U=",
    "P-garbage": "Bearer not-a-jwt",
    "P-token-with-trailing-words": "Bearer a.b.c extra",
}


@pytest.mark.parametrize("attack", list(ATTACKS))
def test_forged_foreign_unsigned_expired_or_tenantless_token_never_reaches_the_database(
    client: TestClient, world: dict[str, Row], token_settings: Settings, jwt_keys: RsaKeyPair,
    executed: list[uuid.UUID], attack: str,
) -> None:
    token = ATTACKS[attack](world, token_settings, jwt_keys)

    response = _send(client, "users", token, claimed=str(world["B"]["id"]), role="COMPANY_ADMIN")

    assert response.status_code == 401
    assert executed == []
    _assert_no_data_leaked(response)


@pytest.mark.parametrize("attack", list(MALFORMED_AUTHORIZATION))
def test_missing_or_malformed_authentication_never_reaches_the_database(
    client: TestClient, world: dict[str, Row], executed: list[uuid.UUID], attack: str
) -> None:
    header = MALFORMED_AUTHORIZATION[attack]

    response = _send(client, "users", None, claimed=str(world["B"]["id"]), role="COMPANY_ADMIN",
                     authorization=header)

    assert response.status_code == 401
    assert executed == []
    _assert_no_data_leaked(response)


# --- 4. Q, R: unknown roles and permissions fail closed before the database -----------------------------------


def test_q_authenticated_role_missing_from_the_mapping_is_denied_before_the_database(
    client: TestClient, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType({UserRole.STAFF: ROLE_PERMISSIONS[UserRole.STAFF]}))

    response = _send(client, "users", _token(world["admin_a"], token_settings))

    assert response.status_code == 403
    assert executed == []


def test_r_permission_granted_to_no_role_is_denied_before_the_database(
    client: TestClient, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reduced = {role: granted - {Permission.COMPANY_USER_READ} for role, granted in ROLE_PERMISSIONS.items()}
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType(reduced))

    statuses = [_send(client, "users", _token(world[name], token_settings)).status_code
                for name in ("admin_a", "admin_b", "staff_a")]

    assert statuses == [403, 403, 403]
    assert executed == []
    with pytest.raises(TypeError):
        permission_required("company:user:read")  # type: ignore[arg-type]  # an undeclared permission can't be used


# --- 4. U: an admin cannot reach another company's users through a tenant-scoped operation ---------------------


@pytest.mark.parametrize("company", ["A", "B"])
def test_u_admin_cannot_read_another_companys_user_by_id(
    client: TestClient, world: dict[str, Row], token_settings: Settings, executed: list[uuid.UUID], company: str
) -> None:
    other = OTHER[company].lower()
    token = _token(world[f"admin_{company.lower()}"], token_settings)
    headers = {"Authorization": f"Bearer {token}", "X-Company-Id": str(world[OTHER[company]]["id"])}

    foreign = [client.post(f"/_xt/users/{world[f'{who}_{other}']['id']}", headers=headers,
                           params={"company_id": str(world[OTHER[company]]["id"])}).json()
               for who in ("admin", "staff")]
    own = client.post(f"/_xt/users/{world[f'staff_{company.lower()}']['id']}", headers=headers).json()

    assert foreign == [{"email": None}, {"email": None}]
    assert own == {"email": f"staff@{company.lower()}.example"}
    assert executed == [world[company]["id"]] * 3  # every lookup ran inside the token's company
