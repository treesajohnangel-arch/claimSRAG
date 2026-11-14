"""Sign-in through ``POST /auth/login`` against PostgreSQL (D-032): real credentials, the real Argon2id check, a real
token, and that token used on the tenant's own routes. Only the database connection is the test's (rolled back).
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.auth import SIGN_IN_UNAVAILABLE
from app.api.database import database_connection
from app.auth import GENERIC_FAILURE_MESSAGE
from app.config import Settings
from app.main import create_app
from app.passwords import hash_password
from app.tokens import decode_access_token
from tests.conftest import RsaKeyPair

pytestmark = pytest.mark.database

Row = dict[str, Any]
Conn = psycopg.Connection[Any]
PASSWORD = "correct horse battery staple 7"  # a fake test credential
RESPONSE_FIELDS = {"access_token", "token_type", "expires_in", "user"}


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem,
                    access_token_lifetime_minutes=20)


@pytest.fixture
def accounts(insert_company: Callable[..., Row], insert_user: Callable[..., Row], insert_policy: Callable[..., Row],
             password_hash: str) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for name in ("A", "B"):
        rows[name] = insert_company(name=f"Company {name}", slug=f"login-{name.lower()}")
        rows[f"{name}-policy"] = insert_policy(company_id=rows[name]["id"], policy_number=f"{name}-1",
                                               name=f"Policy of {name}")
    rows["A-admin"] = insert_user(company_id=rows["A"]["id"], email="admin@a.example", role="COMPANY_ADMIN",
                                  full_name="Asha Admin", password_hash=password_hash)
    rows["A-staff"] = insert_user(company_id=rows["A"]["id"], email="staff@a.example", role="STAFF",
                                  full_name="Sam Staff", password_hash=password_hash)
    rows["B-staff"] = insert_user(company_id=rows["B"]["id"], email="staff@b.example", role="STAFF",
                                  full_name="Bea Staff", password_hash=password_hash)
    rows["A-inactive"] = insert_user(company_id=rows["A"]["id"], email="gone@a.example", status="INACTIVE",
                                     password_hash=password_hash)
    rows["C"] = insert_company(name="Company C", slug="login-c", status="INACTIVE")
    rows["C-staff"] = insert_user(company_id=rows["C"]["id"], email="staff@c.example", password_hash=password_hash)
    return rows


def _client(settings: Settings, db: Conn) -> TestClient:
    app = create_app(settings)

    def connection() -> Iterator[Conn]:
        yield db

    app.dependency_overrides[database_connection] = connection
    return TestClient(app)


@pytest.fixture
def api(token_settings: Settings, db: Conn) -> Iterator[TestClient]:
    with _client(token_settings, db) as client:
        yield client


@pytest.mark.security
@pytest.mark.parametrize(("account", "slug", "email", "role"),
                         [("A-admin", "login-a", "admin@a.example", "COMPANY_ADMIN"),
                          ("A-staff", "login-a", "  Staff@A.Example ", "STAFF"),
                          ("B-staff", "LOGIN-B", "staff@b.example", "STAFF")],
                         ids=["admin", "staff-email-normalised", "other-company-slug-case"])
def test_valid_credentials_get_a_token_for_the_stored_user_company_and_role(
    api: TestClient, accounts: dict[str, Row], token_settings: Settings, account: str, slug: str, email: str,
    role: str,
) -> None:
    response = api.post("/auth/login", json={"company_slug": slug, "email": email, "password": PASSWORD})

    body = response.json()
    assert response.status_code == 200 and set(body) == RESPONSE_FIELDS
    assert (body["token_type"], body["expires_in"]) == ("bearer", 20 * 60)
    user = accounts[account]
    assert body["user"] == {"full_name": user["full_name"], "email": user["email"], "role": role}
    identity = decode_access_token(body["access_token"], token_settings)
    assert (identity.user_id, identity.company_id, identity.role) == (user["id"], user["company_id"], role)
    for hidden in ("company_id", "password", "status", str(user["company_id"])):
        assert hidden not in str(body["user"]) and hidden not in {k for k in body if k != "access_token"}


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_the_token_reaches_only_the_signed_in_users_own_company(
    api: TestClient, accounts: dict[str, Row], company: str
) -> None:
    email = {"A": "staff@a.example", "B": "staff@b.example"}[company]
    token = api.post("/auth/login", json={"company_slug": f"login-{company.lower()}", "email": email,
                                          "password": PASSWORD}).json()["access_token"]

    response = api.get("/policies", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert [policy["policy_number"] for policy in response.json()] == [f"{company}-1"]


@pytest.mark.security
@pytest.mark.parametrize(
    "credentials",
    [{"company_slug": "login-a", "email": "staff@a.example", "password": "wrong password"},
     {"company_slug": "login-a", "email": "nobody@a.example", "password": PASSWORD},
     {"company_slug": "login-zzz", "email": "staff@a.example", "password": PASSWORD},
     {"company_slug": "login-b", "email": "staff@a.example", "password": PASSWORD},
     {"company_slug": "login-a", "email": "gone@a.example", "password": PASSWORD},
     {"company_slug": "login-c", "email": "staff@c.example", "password": PASSWORD},
     {"company_slug": "login-a", "email": "not an email", "password": PASSWORD}],
    ids=["wrong-password", "unknown-user", "unknown-company", "another-companys-slug", "inactive-user",
         "inactive-company", "invalid-email"],
)
def test_every_failed_sign_in_is_the_same_401_and_never_echoes_the_credentials(
    api: TestClient, accounts: dict[str, Row], credentials: dict[str, str]
) -> None:
    response = api.post("/auth/login", json=credentials)

    assert (response.status_code, response.json()) == (401, {"detail": GENERIC_FAILURE_MESSAGE})
    assert PASSWORD not in response.text and "access_token" not in response.text


@pytest.mark.security
@pytest.mark.parametrize(
    ("credentials", "missing"),
    [({"company_slug": "login-a", "email": "staff@a.example", "password": ""}, ["password"]),
     ({"company_slug": "login-a", "email": "staff@a.example"}, ["password"]),
     ({"email": "staff@a.example", "password": PASSWORD}, ["company_slug"]),
     ({"company_slug": "", "email": "", "password": PASSWORD}, ["company_slug", "email"]),
     ({}, ["company_slug", "email", "password"])],
    ids=["empty-password", "missing-password", "missing-company", "empty-company-and-email", "empty-body"],
)
def test_missing_sign_in_fields_are_a_422_naming_them_without_echoing_anything(
    api: TestClient, accounts: dict[str, Row], credentials: dict[str, str], missing: list[str]
) -> None:
    # A validation error, as for any request, but in a fixed shape: FastAPI's own 422 would repeat the whole body,
    # password included, as its "input".
    response = api.post("/auth/login", json=credentials)

    assert response.status_code == 422
    assert response.json() == {"detail": [{"loc": ["body", field], "msg": "Field required", "type": "value_error"}
                                          for field in missing]}
    assert PASSWORD not in response.text and "staff@a.example" not in response.text
    assert "access_token" not in response.text


@pytest.mark.security
@pytest.mark.parametrize(
    "extra",
    [{"company_id": "11111111-1111-4111-8111-111111111111"}, {"role": "COMPANY_ADMIN"}, {"tenant_id": "x"},
     {"permissions": ["policy:manage"]}],
    ids=["company-id", "role", "tenant-id", "permissions"],
)
def test_a_client_cannot_choose_its_company_role_or_permissions_at_sign_in(
    api: TestClient, accounts: dict[str, Row], token_settings: Settings, extra: dict[str, Any]
) -> None:
    credentials = {"company_slug": "login-a", "email": "staff@a.example", "password": PASSWORD}

    in_body = api.post("/auth/login", json=credentials | extra)
    elsewhere = api.post("/auth/login", json=credentials, params={k: str(v) for k, v in extra.items()},
                         headers={"X-Role": "COMPANY_ADMIN", "X-Company-Id": str(accounts["B"]["id"])})

    assert in_body.status_code == 422 and PASSWORD not in in_body.text
    identity = decode_access_token(elsewhere.json()["access_token"], token_settings)
    assert (identity.company_id, identity.role) == (accounts["A"]["id"], "STAFF")  # the stored row decides


@pytest.mark.security
def test_a_mistyped_body_is_a_422_that_never_echoes_the_password(api: TestClient, accounts: dict[str, Row]) -> None:
    for body in ({"company_slug": "login-a", "email": 7, "password": PASSWORD},
                 {"company_slug": ["login-a"], "email": "staff@a.example", "password": PASSWORD}):
        response = api.post("/auth/login", json=body)

        assert response.status_code == 422 and PASSWORD not in response.text


@pytest.mark.security
def test_sign_in_without_signing_keys_is_a_generic_503(db: Conn, accounts: dict[str, Row]) -> None:
    with _client(Settings(_env_file=None), db) as client:
        response = client.post("/auth/login", json={"company_slug": "login-a", "email": "staff@a.example",
                                                    "password": PASSWORD})

    assert (response.status_code, response.json()) == (503, {"detail": SIGN_IN_UNAVAILABLE})


@pytest.mark.security
def test_sign_in_logs_no_password_email_or_token(
    api: TestClient, accounts: dict[str, Row], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        good = api.post("/auth/login", json={"company_slug": "login-a", "email": "staff@a.example",
                                             "password": PASSWORD})
        api.post("/auth/login", json={"company_slug": "login-a", "email": "staff@a.example", "password": "nope-123"})

    assert "authentication succeeded" in caplog.text and "authentication failed reason=wrong_password" in caplog.text
    token = good.json()["access_token"]
    for secret in (PASSWORD, "nope-123", "staff@a.example", token, *token.split(".")[1:]):
        assert secret not in caplog.text
