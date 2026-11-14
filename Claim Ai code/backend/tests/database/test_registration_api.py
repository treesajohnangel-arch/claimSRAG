"""Company registration through ``POST /auth/register-company`` against PostgreSQL (D-035): the company and its
first COMPANY_ADMIN are created together or not at all, the admin can then sign in to that company only, and nothing
the client sends can choose a role, a company id or another tenant. Only the database connection is the test's
(rolled back), so the real transaction dependency still runs.
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import app.registration as registration
from app.api.auth import COMPANY_CODE_TAKEN
from app.api.database import database_connection
from app.config import Settings
from app.main import create_app
from app.tokens import decode_access_token
from tests.conftest import RsaKeyPair

pytestmark = pytest.mark.database

Row = dict[str, Any]
Conn = psycopg.Connection[Any]
PASSWORD = "registration test password 9"  # a fake test credential
VALID = {"company_name": "  Acme Health Insurance ", "company_slug": "Acme-Health", "admin_full_name": " Asha Menon ",
         "admin_email": "  Admin@Acme.Example ", "admin_password": PASSWORD}


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def api(token_settings: Settings, db: Conn) -> Iterator[TestClient]:
    app = create_app(token_settings)

    def connection() -> Iterator[Conn]:
        yield db

    app.dependency_overrides[database_connection] = connection
    with TestClient(app) as client:
        yield client


def _company(db: Conn, slug: str) -> Row | None:
    return db.execute("SELECT id, name, slug, status FROM companies WHERE slug = %s", (slug,)).fetchone()


def _users(db: Conn, company_id: Any) -> list[Row]:
    return db.execute("SELECT email, full_name, role, status, password_hash FROM users WHERE company_id = %s",
                      (company_id,)).fetchall()


@pytest.mark.security
def test_registration_creates_the_company_and_its_first_admin_who_can_then_sign_in(
    api: TestClient, db: Conn, token_settings: Settings
) -> None:
    response = api.post("/auth/register-company", json=VALID)

    assert response.status_code == 201
    assert response.json() == {"company": {"name": "Acme Health Insurance", "slug": "acme-health"},
                               "admin": {"full_name": "Asha Menon", "email": "admin@acme.example",
                                         "role": "COMPANY_ADMIN"}}
    company = _company(db, "acme-health")
    assert company is not None and company["status"] == "ACTIVE"
    (user,) = _users(db, company["id"])
    assert (user["email"], user["full_name"], user["role"], user["status"]) == (
        "admin@acme.example", "Asha Menon", "COMPANY_ADMIN", "ACTIVE")
    assert user["password_hash"].startswith("$argon2id$") and PASSWORD not in user["password_hash"]
    for hidden in ("password", "hash", "argon2", str(company["id"])):
        assert hidden not in response.text

    login = api.post("/auth/login", json={"company_slug": "acme-health", "email": "Admin@Acme.Example",
                                          "password": PASSWORD})
    identity = decode_access_token(login.json()["access_token"], token_settings)
    assert (identity.company_id, identity.role) == (company["id"], "COMPANY_ADMIN")


@pytest.mark.security
def test_a_new_company_sees_only_its_own_empty_workspace(
    api: TestClient, insert_company: Callable[..., Row], insert_policy: Callable[..., Row]
) -> None:
    other = insert_company(slug="existing-co")
    insert_policy(company_id=other["id"], policy_number="SECRET-1", name="Another company's policy")
    api.post("/auth/register-company", json=VALID)
    token = api.post("/auth/login", json={"company_slug": "acme-health", "email": "admin@acme.example",
                                          "password": PASSWORD}).json()["access_token"]

    response = api.get("/policies", headers={"Authorization": f"Bearer {token}"})

    assert (response.status_code, response.json()) == (200, [])
    assert "SECRET-1" not in response.text


@pytest.mark.security
def test_a_company_code_that_is_taken_is_a_409_and_creates_nothing(
    api: TestClient, db: Conn, insert_company: Callable[..., Row]
) -> None:
    existing = insert_company(slug="acme-health", name="The Original Acme")

    response = api.post("/auth/register-company", json=VALID)

    assert (response.status_code, response.json()) == (409, {"detail": COMPANY_CODE_TAKEN})
    assert _company(db, "acme-health")["name"] == "The Original Acme"  # type: ignore[index]
    assert _users(db, existing["id"]) == []  # no admin was added to the existing company
    assert PASSWORD not in response.text


def test_the_same_email_may_administer_another_company(api: TestClient) -> None:
    # Emails are unique within a company (D-013), and sign-in names the company, so one person may register two.
    first = api.post("/auth/register-company", json=VALID)
    second = api.post("/auth/register-company", json={**VALID, "company_slug": "acme-motor"})

    assert (first.status_code, second.status_code) == (201, 201)


@pytest.mark.security
@pytest.mark.parametrize(
    ("changes", "fields"),
    [({"company_name": "   "}, ["company_name"]),
     ({"company_name": "x" * 201}, ["company_name"]),
     ({"company_slug": "acme health"}, ["company_slug"]),
     ({"company_slug": "-acme"}, ["company_slug"]),
     ({"company_slug": "a" * 64}, ["company_slug"]),
     ({"admin_full_name": ""}, ["admin_full_name"]),
     ({"admin_email": "not-an-email"}, ["admin_email"]),
     ({"admin_password": "short"}, ["admin_password"]),
     ({"admin_password": " " * 20}, ["admin_password"]),
     ({"admin_password": "x" * 257}, ["admin_password"]),
     ({"company_name": "", "company_slug": "", "admin_full_name": "", "admin_email": "", "admin_password": ""},
      ["admin_email", "admin_full_name", "admin_password", "company_name", "company_slug"])],
    ids=["blank-name", "long-name", "slug-with-space", "slug-leading-hyphen", "long-slug", "no-admin-name",
         "bad-email", "short-password", "spaces-password", "long-password", "everything-empty"],
)
def test_invalid_details_are_a_422_naming_each_field_without_echoing_and_create_nothing(
    api: TestClient, db: Conn, changes: dict[str, str], fields: list[str]
) -> None:
    response = api.post("/auth/register-company", json={**VALID, **changes})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert [item["loc"] for item in detail] == [["body", field] for field in fields]
    assert all(set(item) == {"loc", "msg", "type"} for item in detail)  # never the input
    assert PASSWORD not in response.text
    assert db.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 0  # type: ignore[index]


@pytest.mark.security
@pytest.mark.parametrize(
    "extra",
    [{"role": "STAFF"}, {"role": "SUPER_ADMIN"}, {"company_id": "11111111-1111-4111-8111-111111111111"},
     {"status": "INACTIVE"}, {"permissions": ["policy:manage"]}, {"tenant_id": "x"}],
    ids=["role-staff", "role-invented", "company-id", "status", "permissions", "tenant-id"],
)
def test_the_client_cannot_choose_a_role_company_status_or_permissions(
    api: TestClient, db: Conn, extra: dict[str, Any]
) -> None:
    response = api.post("/auth/register-company", json=VALID | extra)

    assert response.status_code == 422 and PASSWORD not in response.text
    assert db.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 0  # type: ignore[index]


@pytest.mark.security
def test_a_failure_part_way_leaves_neither_company_nor_admin(
    api: TestClient, db: Conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the user insert to fail inside the single statement (an email the database's CHECK refuses): the company
    # inserted by the same statement must not survive.
    real = registration._validated
    monkeypatch.setattr(registration, "_validated", lambda *args: {**real(*args), "email": "no at sign"})

    response = api.post("/auth/register-company", json=VALID)

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert _company(db, "acme-health") is None
    assert db.execute("SELECT count(*) AS n FROM users").fetchone()["n"] == 0  # type: ignore[index]


@pytest.mark.security
def test_registration_logs_ids_only(api: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        api.post("/auth/register-company", json=VALID)
        api.post("/auth/register-company", json=VALID)  # taken

    assert "company registered company_id=" in caplog.text
    assert "company registration refused reason=company_code_taken" in caplog.text
    for secret in (PASSWORD, "admin@acme.example", "Admin@Acme.Example", "Asha Menon", "$argon2id$"):
        assert secret not in caplog.text
