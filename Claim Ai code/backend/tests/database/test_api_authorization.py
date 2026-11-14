"""A fully protected route against the real schema: authentication, RBAC, tenant resolution and tenant-aware data
access together. Authorization never widens the tenant: COMPANY_ADMIN of A reads only A.

The route is test-only (a test app instance). Data lives in a rolled-back transaction in the throwaway test database.
"""

import uuid
from collections.abc import Callable
from typing import Annotated, Any

import psycopg
import pytest
from fastapi import Body, Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.authorization import permission_required
from app.api.tenancy import current_tenant
from app.auth import AuthenticatedUser
from app.config import Settings
from app.main import create_app
from app.models import UserRole, UserStatus
from app.rbac import FORBIDDEN_MESSAGE, Permission
from app.tenancy import TenantIdentity
from app.tenant_db import TenantDatabase
from app.tokens import issue_access_token
from tests.conftest import RsaKeyPair

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]


@pytest.fixture
def people(insert_company: Insert, insert_user: Insert) -> dict[str, Row]:
    a, b = insert_company(name="Company A", slug="authz-a"), insert_company(name="Company B", slug="authz-b")
    return {
        "admin_a": insert_user(company_id=a["id"], email="admin@a.example", role="COMPANY_ADMIN"),
        "staff_a": insert_user(company_id=a["id"], email="staff@a.example", role="STAFF"),
        "admin_b": insert_user(company_id=b["id"], email="admin@b.example", role="COMPANY_ADMIN"),
        "staff_b": insert_user(company_id=b["id"], email="staff@b.example", role="STAFF"),
    }


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


def _app(settings: Settings, db: Conn, queries: list[uuid.UUID]) -> FastAPI:
    app = create_app(settings)

    @app.post("/_company_users_test", dependencies=[Depends(permission_required(Permission.COMPANY_USER_READ))])
    def company_users(
        tenant: Annotated[TenantIdentity, Depends(current_tenant)],
        company_id: str | None = None,
        payload: Annotated[dict[str, Any] | None, Body()] = None,
    ) -> list[str]:
        queries.append(tenant.company_id)
        return sorted(user.email for user in TenantDatabase(db, tenant).list_users())

    return app


def _token(row: Row, settings: Settings) -> str:
    user = AuthenticatedUser(user_id=row["id"], company_id=row["company_id"], email=row["email"],
                             full_name=row["full_name"], role=UserRole(row["role"]), status=UserStatus.ACTIVE)
    return issue_access_token(user, settings)


@pytest.mark.security
@pytest.mark.parametrize(("admin", "expected"), [("admin_a", ["admin@a.example", "staff@a.example"]),
                                                 ("admin_b", ["admin@b.example", "staff@b.example"])])
def test_company_admin_reads_only_its_own_company(
    db: Conn, people: dict[str, Row], token_settings: Settings, admin: str, expected: list[str]
) -> None:
    with TestClient(_app(token_settings, db, [])) as client:
        response = client.post("/_company_users_test", headers={"Authorization": f"Bearer {_token(people[admin], token_settings)}"})

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.security
def test_company_admin_cannot_reach_another_company_with_client_values(
    db: Conn, people: dict[str, Row], token_settings: Settings
) -> None:
    company_b = str(people["admin_b"]["company_id"])
    headers = {"Authorization": f"Bearer {_token(people['admin_a'], token_settings)}", "X-Company-Id": company_b,
               "X-Role": "SUPER_ADMIN", "Cookie": f"company_id={company_b}"}

    with TestClient(_app(token_settings, db, [])) as client:
        response = client.post("/_company_users_test", headers=headers, params={"company_id": company_b},
                               json={"company_id": company_b, "role": "SUPER_ADMIN"})

    assert response.json() == ["admin@a.example", "staff@a.example"]


@pytest.mark.security
@pytest.mark.parametrize("staff", ["staff_a", "staff_b"])
def test_staff_without_the_permission_is_403_and_never_reaches_the_database(
    db: Conn, people: dict[str, Row], token_settings: Settings, staff: str
) -> None:
    queries: list[uuid.UUID] = []

    with TestClient(_app(token_settings, db, queries)) as client:
        response = client.post("/_company_users_test", headers={"Authorization": f"Bearer {_token(people[staff], token_settings)}"},
                               params={"role": "COMPANY_ADMIN"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_MESSAGE}
    assert queries == []
