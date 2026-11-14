"""The development-only demo session, ``POST /auth/demo-session`` (D-036), against PostgreSQL.

It issues a normal access token for exactly the user and company named by ``DEMO_USER_EMAIL`` and
``DEMO_COMPANY_SLUG``, and nothing else: no client input reaches it, the token's company and role come from the stored
rows, and that token reaches only that company's data through the unchanged tenant boundary. Only the database
connection is the test's (rolled back).
"""

import logging
from collections.abc import Callable, Iterator
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.auth import DEMO_UNAVAILABLE
from app.api.database import database_connection
from app.config import Settings
from app.main import create_app
from app.tokens import decode_access_token
from tests.conftest import RsaKeyPair

pytestmark = pytest.mark.database

Row = dict[str, Any]
Conn = psycopg.Connection[Any]
RESPONSE_FIELDS = {"access_token", "token_type", "expires_in", "user", "company"}


def _settings(jwt_keys: RsaKeyPair | None, **demo: str) -> Settings:
    keys = {} if jwt_keys is None else {"jwt_private_key": jwt_keys.private_pem, "jwt_public_key": jwt_keys.public_pem}
    return Settings(_env_file=None, access_token_lifetime_minutes=20, **keys, **demo)


@pytest.fixture
def accounts(insert_company: Callable[..., Row], insert_user: Callable[..., Row],
             insert_policy: Callable[..., Row]) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for name in ("A", "B"):
        rows[name] = insert_company(name=f"Demo Company {name}", slug=f"demo-{name.lower()}")
        rows[f"{name}-policy"] = insert_policy(company_id=rows[name]["id"], policy_number=f"{name}-1",
                                               name=f"Policy of {name}")
    rows["A-admin"] = insert_user(company_id=rows["A"]["id"], email="admin@a.example", role="COMPANY_ADMIN",
                                  full_name="Asha Admin")
    rows["A-staff"] = insert_user(company_id=rows["A"]["id"], email="staff@a.example", role="STAFF",
                                  full_name="Sam Staff")
    rows["B-admin"] = insert_user(company_id=rows["B"]["id"], email="admin@b.example", role="COMPANY_ADMIN")
    rows["A-inactive"] = insert_user(company_id=rows["A"]["id"], email="gone@a.example", status="INACTIVE")
    rows["C"] = insert_company(name="Demo Company C", slug="demo-c", status="INACTIVE")
    rows["C-admin"] = insert_user(company_id=rows["C"]["id"], email="admin@c.example", role="COMPANY_ADMIN")
    return rows


@pytest.fixture
def client_for(db: Conn) -> Iterator[Callable[[Settings], TestClient]]:
    clients: list[TestClient] = []

    def make(settings: Settings) -> TestClient:
        app = create_app(settings)

        def connection() -> Iterator[Conn]:
            yield db

        app.dependency_overrides[database_connection] = connection
        client = TestClient(app)
        clients.append(client.__enter__())
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


@pytest.mark.security
def test_demo_access_is_off_unless_configured(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair
) -> None:
    response = client_for(_settings(jwt_keys)).post("/auth/demo-session")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}  # the same as a route that does not exist


@pytest.mark.security
@pytest.mark.parametrize(("slug", "email", "account", "company"),
                         [("demo-a", "admin@a.example", "A-admin", "A"),
                          ("demo-a", "  Staff@A.Example ", "A-staff", "A"),
                          ("DEMO-B", "admin@b.example", "B-admin", "B")],
                         ids=["admin", "staff-email-normalised", "other-company"])
def test_the_token_is_for_the_configured_user_and_its_stored_company_and_role(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair,
    slug: str, email: str, account: str, company: str,
) -> None:
    settings = _settings(jwt_keys, demo_company_slug=slug, demo_user_email=email)

    response = client_for(settings).post("/auth/demo-session")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == RESPONSE_FIELDS
    assert body["token_type"] == "bearer" and body["expires_in"] == 20 * 60
    user = accounts[account]
    assert body["user"] == {"full_name": user["full_name"], "email": user["email"], "role": user["role"]}
    assert body["company"] == {"name": accounts[company]["name"], "slug": accounts[company]["slug"]}
    identity = decode_access_token(body["access_token"], settings)
    assert (identity.user_id, identity.company_id, identity.role) == (user["id"], user["company_id"], user["role"])
    assert str(user["company_id"]) not in response.text and "status" not in response.text


@pytest.mark.security
def test_the_demo_token_reaches_only_the_demo_company(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair
) -> None:
    client = client_for(_settings(jwt_keys, demo_company_slug="demo-a", demo_user_email="admin@a.example"))
    headers = {"Authorization": f"Bearer {client.post('/auth/demo-session').json()['access_token']}"}

    listed = client.get("/policies", headers=headers)
    other = client.get(f"/policies/{accounts['B-policy']['id']}", headers=headers)
    missing = client.get(f"/policies/{uuid4()}", headers=headers)

    assert [policy["policy_number"] for policy in listed.json()] == ["A-1"]
    # Another company's policy is exactly a missing one, as with a signed-in token.
    assert (other.status_code, other.json()) == (missing.status_code, missing.json()) == (404, missing.json())
    assert missing.status_code == 404


@pytest.mark.security
def test_a_client_cannot_choose_the_demo_company_user_or_role(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair
) -> None:
    settings = _settings(jwt_keys, demo_company_slug="demo-a", demo_user_email="staff@a.example")
    client = client_for(settings)
    attempts = {"company_slug": "demo-b", "company_id": str(accounts["B"]["id"]), "email": "admin@b.example",
                "role": "COMPANY_ADMIN", "tenant": str(accounts["B"]["id"])}

    client.cookies.set("company_id", str(accounts["B"]["id"]))

    response = client.post("/auth/demo-session", params=attempts, json=attempts,
                           headers={"X-Company-Id": str(accounts["B"]["id"]), "X-Role": "COMPANY_ADMIN"})

    assert response.status_code == 200
    identity = decode_access_token(response.json()["access_token"], settings)
    assert (identity.user_id, identity.company_id, identity.role) == (
        accounts["A-staff"]["id"], accounts["A"]["id"], "STAFF")


@pytest.mark.security
@pytest.mark.parametrize(("slug", "email"),
                         [("demo-a", "nobody@a.example"),    # no such user
                          ("demo-zzz", "admin@a.example"),   # no such company
                          ("demo-a", "admin@b.example"),     # a user of another company
                          ("demo-a", "gone@a.example"),      # an inactive user
                          ("demo-c", "admin@c.example"),     # a user of an inactive company
                          ("demo-a", "not an email")],
                         ids=["unknown-user", "unknown-company", "other-company-user", "inactive-user",
                              "inactive-company", "invalid-email"])
def test_an_unavailable_demo_account_is_one_generic_503(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair,
    slug: str, email: str,
) -> None:
    response = client_for(_settings(jwt_keys, demo_company_slug=slug, demo_user_email=email)).post(
        "/auth/demo-session")

    assert response.status_code == 503
    assert response.json() == {"detail": DEMO_UNAVAILABLE}
    assert "access_token" not in response.text


def test_demo_access_without_signing_keys_is_the_same_503(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row]
) -> None:
    response = client_for(_settings(None, demo_company_slug="demo-a", demo_user_email="admin@a.example")).post(
        "/auth/demo-session")

    assert response.status_code == 503
    assert response.json() == {"detail": DEMO_UNAVAILABLE}


def test_only_post_is_served(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair
) -> None:
    client = client_for(_settings(jwt_keys, demo_company_slug="demo-a", demo_user_email="admin@a.example"))

    assert client.get("/auth/demo-session").status_code == 405


@pytest.mark.security
def test_the_demo_session_logs_no_email_or_token(
    client_for: Callable[[Settings], TestClient], accounts: dict[str, Row], jwt_keys: RsaKeyPair,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        token = client_for(_settings(jwt_keys, demo_company_slug="demo-a", demo_user_email="admin@a.example")).post(
            "/auth/demo-session").json()["access_token"]
        client_for(_settings(jwt_keys, demo_company_slug="demo-a", demo_user_email="gone@a.example")).post(
            "/auth/demo-session")

    assert f"demo session issued user_id={accounts['A-admin']['id']}" in caplog.text
    assert f"demo session unavailable reason=inactive_user user_id={accounts['A-inactive']['id']}" in caplog.text
    for secret in (token, "admin@a.example", "gone@a.example"):
        assert secret not in caplog.text
