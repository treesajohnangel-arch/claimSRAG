"""Tenant-aware database access against the real schema (users is the only tenant-owned table today).

Company A has users A1 (COMPANY_ADMIN) and A2 (STAFF); company B has B1 (COMPANY_ADMIN) and B2 (STAFF). Every test
runs in a transaction that is rolled back, in the throwaway test database. All credentials are fake.
"""

import uuid
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
import pytest
from fastapi import Body, Cookie, Depends, FastAPI, Header
from fastapi.testclient import TestClient

from app.api.tenancy import current_tenant
from app.auth import AuthenticatedUser, authenticate
from app.config import Settings
from app.main import create_app
from app.models import UserRole, UserStatus
from app.passwords import hash_password
from app.rbac import Permission, has_permission
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase, TenantScopeError
from app.tokens import decode_access_token, issue_access_token
from tests.conftest import RsaKeyPair, forge_claims
from tests.database.conftest import PASSWORD_HASH

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]

PASSWORD = "fake-Passw0rd-for-tenant-tests"
TRANSPORTS = ["path", "query", "body", "header", "cookie"]


@pytest.fixture
def tenants(insert_company: Insert, insert_user: Insert) -> dict[str, Row]:
    a = insert_company(name="Company A", slug="tenant-a")
    b = insert_company(name="Company B", slug="tenant-b")
    return {
        "A": a,
        "B": b,
        "A1": insert_user(company_id=a["id"], email="a1@a.example", role="COMPANY_ADMIN"),
        "A2": insert_user(company_id=a["id"], email="a2@a.example", role="STAFF"),
        "B1": insert_user(company_id=b["id"], email="b1@b.example", role="COMPANY_ADMIN"),
        "B2": insert_user(company_id=b["id"], email="b2@b.example", role="STAFF"),
    }


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


def _scoped(db: Conn, company_id: uuid.UUID) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company_id))


def _ids(users: list[Any]) -> set[uuid.UUID]:
    return {user.id for user in users}


def _authenticated(tenants: dict[str, Row], name: str) -> AuthenticatedUser:
    """The identity authenticate() would return for this test user."""
    row = tenants[name]
    return AuthenticatedUser(user_id=row["id"], company_id=row["company_id"], email=row["email"],
                             full_name=row["full_name"], role=UserRole(row["role"]), status=UserStatus.ACTIVE)


# --- Each tenant sees exactly its own rows --------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(("company", "members"), [("A", ("A1", "A2")), ("B", ("B1", "B2"))])
def test_tenant_sees_exactly_its_own_users(db: Conn, tenants: dict[str, Row], company: str, members: tuple[str, ...]) -> None:
    users = _scoped(db, tenants[company]["id"]).list_users()

    assert _ids(users) == {tenants[name]["id"] for name in members}
    assert {user.company_id for user in users} == {tenants[company]["id"]}


@pytest.mark.security
def test_a_and_b_cannot_read_each_other(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a, tenant_b = _scoped(db, tenants["A"]["id"]), _scoped(db, tenants["B"]["id"])

    assert _ids(tenant_a.list_users()).isdisjoint(_ids(tenant_b.list_users()))
    for name in ("B1", "B2"):
        assert tenant_a.get_user(tenants[name]["id"]) is None  # A asking for B's user by id gets nothing
    for name in ("A1", "A2"):
        assert tenant_b.get_user(tenants[name]["id"]) is None
    found = tenant_a.get_user(tenants["A1"]["id"])
    assert found is not None and found.email == "a1@a.example"


@pytest.mark.security
def test_attempted_company_id_b_cannot_become_the_query_tenant(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"]["id"])

    with pytest.raises(TypeError):
        tenant_a.list_users(company_id=tenants["B"]["id"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.get_user(tenants["B1"]["id"], company_id=tenants["B"]["id"])  # type: ignore[call-arg]

    assert _ids(tenant_a.list_users()) == {tenants["A1"]["id"], tenants["A2"]["id"]}


@pytest.mark.security
def test_company_admin_cannot_bypass_the_tenant_boundary(db: Conn, tenants: dict[str, Row], token_settings: Settings) -> None:
    identity = decode_access_token(issue_access_token(_authenticated(tenants, "A1"), token_settings), token_settings)

    users = TenantDatabase(db, resolve_tenant(identity)).list_users()

    # RBAC lets the admin read company users ... but only its own company's.
    assert has_permission(identity, Permission.COMPANY_USER_READ)
    assert _ids(users) == {tenants["A1"]["id"], tenants["A2"]["id"]}
    assert TenantDatabase(db, resolve_tenant(identity)).get_user(tenants["B1"]["id"]) is None


@pytest.mark.security
def test_full_chain_from_credentials_reaches_only_the_users_own_tenant(
    db: Conn, tenants: dict[str, Row], token_settings: Settings
) -> None:
    db.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(PASSWORD), tenants["B2"]["id"]))

    authenticated = authenticate(db, "tenant-b", "b2@b.example", PASSWORD)
    identity = decode_access_token(issue_access_token(authenticated, token_settings), token_settings)
    users = TenantDatabase(db, resolve_tenant(identity)).list_users()

    assert identity.company_id == tenants["B"]["id"]
    assert _ids(users) == {tenants["B1"]["id"], tenants["B2"]["id"]}


# --- No fallback ----------------------------------------------------------------------------------------------


@pytest.mark.security
def test_tenant_without_users_and_unknown_tenant_get_nothing_not_a_fallback(
    db: Conn, tenants: dict[str, Row], insert_company: Insert
) -> None:
    empty_company = insert_company(name="Company C", slug="tenant-c")

    assert _scoped(db, empty_company["id"]).list_users() == []
    assert _scoped(db, uuid.uuid4()).list_users() == []


@pytest.mark.security
@pytest.mark.parametrize("tenant", [None, "tenant-a", TenantIdentity.model_construct(company_id=uuid.UUID(int=0))],
                         ids=["none", "slug-string", "nil-tenant"])
def test_missing_or_invalid_tenant_fails_closed(db: Conn, tenants: dict[str, Row], tenant: Any) -> None:
    with pytest.raises(TenantScopeError):
        TenantDatabase(db, tenant)

    # The connection is unaffected: nothing ran, so nothing failed.
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] >= 4  # type: ignore[index]


# --- SQL safety and returned data -----------------------------------------------------------------------------


@pytest.mark.security
def test_hostile_text_in_data_is_handled_as_data(db: Conn, tenants: dict[str, Row], insert_user: Insert) -> None:
    hostile = insert_user(company_id=tenants["A"]["id"], email="o'brien@a.example",
                          full_name="Robert'); DROP TABLE users; --")

    users = _scoped(db, tenants["A"]["id"]).list_users()

    assert hostile["id"] in _ids(users)
    assert next(user for user in users if user.id == hostile["id"]).full_name == "Robert'); DROP TABLE users; --"
    assert _ids(_scoped(db, tenants["B"]["id"]).list_users()) == {tenants["B1"]["id"], tenants["B2"]["id"]}


@pytest.mark.security
def test_tenant_reads_never_return_password_hashes(db: Conn, tenants: dict[str, Row]) -> None:
    users = _scoped(db, tenants["A"]["id"]).list_users()

    for user in users:
        assert "password_hash" not in user.model_dump()
        assert PASSWORD_HASH not in repr(user) and PASSWORD_HASH not in user.model_dump_json()


# --- Over HTTP: the client cannot choose the query tenant -----------------------------------------------------


def _tenant_db_app(settings: Settings, db: Conn) -> FastAPI:
    app = create_app(settings)

    @app.post("/_tenant_db_test/{path_company_id}")
    def list_tenant_users(
        tenant: Annotated[TenantIdentity, Depends(current_tenant)],
        path_company_id: str,
        company_id: str | None = None,
        x_company_id: Annotated[str | None, Header()] = None,
        cookie_company_id: Annotated[str | None, Cookie(alias="company_id")] = None,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> dict[str, Any]:
        # The client's company ids are accepted by the route but never passed on: only the resolved tenant is.
        return {"users": sorted(str(user.id) for user in TenantDatabase(db, tenant).list_users())}

    return app


@pytest.mark.security
@pytest.mark.parametrize("transport", [*TRANSPORTS, "all"])
def test_client_company_id_cannot_choose_the_query_tenant_over_http(
    db: Conn, tenants: dict[str, Row], token_settings: Settings, transport: str
) -> None:
    user_a = _authenticated(tenants, "A2")
    token = issue_access_token(user_a, token_settings)
    company_b = str(tenants["B"]["id"])
    used = TRANSPORTS if transport == "all" else [transport]
    headers = {"Authorization": f"Bearer {token}"}
    if "header" in used:
        headers["X-Company-Id"] = company_b
    if "cookie" in used:
        headers["Cookie"] = f"company_id={company_b}"

    with TestClient(_tenant_db_app(token_settings, db)) as client:
        response = client.post(
            f"/_tenant_db_test/{company_b if 'path' in used else 'unused'}",
            headers=headers,
            params={"company_id": company_b} if "query" in used else None,
            json={"company_id": company_b} if "body" in used else None,
        )

    assert response.status_code == 200
    assert response.json() == {"users": sorted(str(tenants[name]["id"]) for name in ("A1", "A2"))}


@pytest.mark.security
def test_forged_company_in_token_never_reaches_the_database(
    db: Conn, tenants: dict[str, Row], token_settings: Settings
) -> None:
    user_a = _authenticated(tenants, "A2")
    forged = forge_claims(issue_access_token(user_a, token_settings), company_id=str(tenants["B"]["id"]))

    with TestClient(_tenant_db_app(token_settings, db)) as client:
        response = client.post("/_tenant_db_test/unused", headers={"Authorization": f"Bearer {forged}"})

    assert response.status_code == 401
    assert "users" not in response.json()
