"""Authentication against the real Phase 1.1 schema. All credentials here are fake.

These tests cover authentication only. Sessions, RBAC and tenant authorization come in later Phase 1 tasks.
"""

from collections.abc import Callable
from typing import Any

import psycopg
import pytest

import app.auth
from app.auth import GENERIC_FAILURE_MESSAGE, AuthenticatedUser, AuthenticationError, authenticate
from app.config import Settings
from app.emails import normalize_email
from app.logging_config import configure_logging
from app.models import UserRole, UserStatus
from app.passwords import hash_password

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]

PASSWORD = "fake-Passw0rd-for-auth-tests"
OTHER_PASSWORD = "fake-other-Passw0rd"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def account(insert_company: Insert, insert_user: Insert, password_hash: str) -> Row:
    company = insert_company(name="Acme Insurance", slug="acme")
    return insert_user(
        company_id=company["id"],
        email="alice@acme.example",
        password_hash=password_hash,
        full_name="Alice Admin",
        role="COMPANY_ADMIN",
    )


def _failure(conn: Conn, slug: str, email: str, password: str) -> AuthenticationError:
    with pytest.raises(AuthenticationError) as excinfo:
        authenticate(conn, slug, email, password)
    return excinfo.value


# --- Success and the identity returned ------------------------------------------------------------------------


def test_valid_credentials_return_the_users_identity(db: Conn, account: Row) -> None:
    identity = authenticate(db, "acme", "alice@acme.example", PASSWORD)

    assert isinstance(identity, AuthenticatedUser)
    assert identity.user_id == account["id"]
    assert identity.company_id == account["company_id"]
    assert (identity.role, identity.status) == (UserRole.COMPANY_ADMIN, UserStatus.ACTIVE)
    assert (identity.email, identity.full_name) == ("alice@acme.example", "Alice Admin")


@pytest.mark.security
def test_identity_contains_no_password_or_password_hash(db: Conn, account: Row, password_hash: str) -> None:
    identity = authenticate(db, "acme", "alice@acme.example", PASSWORD)

    dumped = identity.model_dump()
    for text in (repr(identity), identity.model_dump_json(), str(dumped)):
        assert PASSWORD not in text
        assert password_hash not in text
    assert "password_hash" not in dumped and "password" not in dumped


def test_email_and_company_slug_are_normalised_before_lookup(db: Conn, account: Row) -> None:
    identity = authenticate(db, "  ACME ", "  Alice@ACME.Example ", PASSWORD)

    assert identity.user_id == account["id"]


# --- Failures are generic -------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    ("slug", "email", "password"),
    [
        ("acme", "alice@acme.example", "wrong-password"),
        ("acme", "nobody@acme.example", PASSWORD),
        ("no-such-company", "alice@acme.example", PASSWORD),
        ("acme", "not-an-email", PASSWORD),
        ("acme", "alice@acme.example", ""),
    ],
    ids=["wrong-password", "unknown-email", "unknown-company", "invalid-email", "empty-password"],
)
def test_every_failure_raises_the_same_generic_error(
    db: Conn, account: Row, slug: str, email: str, password: str
) -> None:
    error = _failure(db, slug, email, password)

    assert str(error) == GENERIC_FAILURE_MESSAGE
    assert error.args == (GENERIC_FAILURE_MESSAGE,)


@pytest.mark.security
def test_unknown_email_and_wrong_password_are_indistinguishable(db: Conn, account: Row) -> None:
    unknown = _failure(db, "acme", "nobody@acme.example", PASSWORD)
    wrong = _failure(db, "acme", "alice@acme.example", "wrong-password")

    assert (type(unknown), unknown.args) == (type(wrong), wrong.args)


@pytest.mark.security
def test_unknown_account_costs_the_same_password_verification(
    db: Conn, account: Row, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_verify = app.auth.verify_password

    def counting_verify(password: str, stored: str) -> bool:
        calls.append("verify")
        return real_verify(password, stored)

    monkeypatch.setattr(app.auth, "verify_password", counting_verify)

    _failure(db, "acme", "nobody@acme.example", PASSWORD)
    unknown_calls = len(calls)
    calls.clear()
    _failure(db, "acme", "alice@acme.example", "wrong-password")

    assert unknown_calls == len(calls) == 1


# --- Status is never bypassed ---------------------------------------------------------------------------------


@pytest.mark.security
def test_inactive_user_cannot_authenticate_with_the_correct_password(db: Conn, account: Row) -> None:
    db.execute("UPDATE users SET status = 'INACTIVE' WHERE id = %s", (account["id"],))

    error = _failure(db, "acme", "alice@acme.example", PASSWORD)

    assert str(error) == GENERIC_FAILURE_MESSAGE


@pytest.mark.security
def test_user_of_an_inactive_company_cannot_authenticate(db: Conn, account: Row) -> None:
    db.execute("UPDATE companies SET status = 'INACTIVE' WHERE id = %s", (account["company_id"],))

    error = _failure(db, "acme", "alice@acme.example", PASSWORD)

    assert str(error) == GENERIC_FAILURE_MESSAGE


# --- Malformed stored hashes ----------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("stored", ["not-a-hash", "$argon2id$v=19$m=65536,t=3,p=4$!!!$!!!"], ids=["plain", "corrupt"])
def test_malformed_stored_hash_fails_safely_without_exposing_it(
    db: Conn,
    insert_company: Insert,
    insert_user: Insert,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
    stored: str,
) -> None:
    company = insert_company(slug="broken-co")
    insert_user(company_id=company["id"], email="bob@broken.example", password_hash=stored)
    configure_logging(settings)

    error = _failure(db, "broken-co", "bob@broken.example", PASSWORD)
    logs = capsys.readouterr().err

    assert str(error) == GENERIC_FAILURE_MESSAGE
    assert stored not in str(error) and stored not in logs
    assert "stored password hash is malformed or unsupported" in logs


# --- Tenant scoping and normalisation -------------------------------------------------------------------------


@pytest.mark.security
def test_same_email_in_two_companies_authenticates_only_into_its_own_company(
    db: Conn, insert_company: Insert, insert_user: Insert, password_hash: str
) -> None:
    company_a = insert_company(slug="company-a")
    company_b = insert_company(slug="company-b")
    user_a = insert_user(company_id=company_a["id"], email="same@example.com", password_hash=password_hash)
    user_b = insert_user(
        company_id=company_b["id"], email="same@example.com", password_hash=hash_password(OTHER_PASSWORD)
    )

    in_a = authenticate(db, "company-a", "same@example.com", PASSWORD)
    in_b = authenticate(db, "company-b", "same@example.com", OTHER_PASSWORD)

    assert (in_a.user_id, in_a.company_id) == (user_a["id"], company_a["id"])
    assert (in_b.user_id, in_b.company_id) == (user_b["id"], company_b["id"])
    # One company's password never opens the other company's account.
    _failure(db, "company-a", "same@example.com", OTHER_PASSWORD)
    _failure(db, "company-b", "same@example.com", PASSWORD)


@pytest.mark.security
def test_lookup_uses_only_the_normalised_email_without_rewriting(db: Conn, account: Row) -> None:
    # Different addresses stay different: no "+tag" or dot rewriting, and no partial or pattern matching.
    for email in ("alice+claims@acme.example", "a.lice@acme.example", "alice@acme.example.", "%@acme.example"):
        _failure(db, "acme", email, PASSWORD)


@pytest.mark.parametrize(
    "raw",
    ["  Alice@Example.com ", "ÉLODIE@EXAMPLE.FR", "First.Last+Tag@Mail.Example.org"],
    ids=["mixed-case", "non-ascii", "dots-and-tag"],
)
def test_normalised_email_is_the_database_representation(db: Conn, insert_user: Insert, raw: str) -> None:
    normalized = normalize_email(raw)

    same = db.execute("SELECT %s = lower(btrim(%s)) AS same", (normalized, normalized)).fetchone()
    user = insert_user(email=normalized)  # passes the users_email_* CHECK constraints

    assert same == {"same": True}
    assert user["email"] == normalized


# --- Logs -----------------------------------------------------------------------------------------------------


@pytest.mark.security
def test_logs_record_the_reason_but_never_credentials(
    db: Conn, account: Row, password_hash: str, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)

    authenticate(db, "acme", "alice@acme.example", PASSWORD)
    _failure(db, "acme", "alice@acme.example", "fake-wrong-guess")
    _failure(db, "acme", "nobody@acme.example", PASSWORD)
    db.execute("UPDATE users SET status = 'INACTIVE' WHERE id = %s", (account["id"],))
    _failure(db, "acme", "alice@acme.example", PASSWORD)
    logs = capsys.readouterr().err

    assert f"authentication succeeded user_id={account['id']} company_id={account['company_id']}" in logs
    assert f"reason=wrong_password user_id={account['id']}" in logs
    assert "reason=unknown_account user_id=None" in logs
    assert f"reason=inactive_user user_id={account['id']}" in logs
    for secret in (PASSWORD, "fake-wrong-guess", password_hash, "alice@acme.example", "nobody@acme.example"):
        assert secret not in logs
