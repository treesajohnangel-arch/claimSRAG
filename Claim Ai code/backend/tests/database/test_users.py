"""Database integrity tests for the users table and its link to companies (migration 0001).

These check the schema only. Application-level tenant isolation is tested in later Phase 1 tasks.
"""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.models import CompanyStatus, User, UserRole, UserStatus
from tests.database.conftest import PASSWORD_HASH

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]


def test_valid_user_is_inserted(insert_company: Insert, insert_user: Insert) -> None:
    company = insert_company()

    user = insert_user(
        company_id=company["id"], email="alice@acme.example", full_name="Alice Admin", role="COMPANY_ADMIN"
    )

    assert isinstance(user["id"], uuid.UUID)
    assert user["id"].version == 4
    assert user["company_id"] == company["id"]
    assert (user["email"], user["full_name"], user["role"], user["status"]) == (
        "alice@acme.example",
        "Alice Admin",
        "COMPANY_ADMIN",
        "ACTIVE",
    )
    assert user["created_at"].tzinfo is not None


def test_user_must_reference_an_existing_company(db: Conn, insert_user: Insert) -> None:
    with pytest.raises(errors.ForeignKeyViolation) as missing_company, db.transaction():
        insert_user(company_id=uuid.uuid4())
    with pytest.raises(errors.NotNullViolation) as no_company, db.transaction():
        insert_user(company_id=None)

    assert missing_company.value.diag.constraint_name == "users_company_id_fkey"
    assert no_company.value.diag.column_name == "company_id"


def test_duplicate_email_in_the_same_company_is_rejected(db: Conn, insert_company: Insert, insert_user: Insert) -> None:
    company = insert_company()
    insert_user(company_id=company["id"], email="alice@example.com")

    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        insert_user(company_id=company["id"], email="alice@example.com")

    assert excinfo.value.diag.constraint_name == "users_company_id_email_key"


def test_same_email_in_two_companies_is_allowed(insert_company: Insert, insert_user: Insert) -> None:
    first, second = insert_company(), insert_company()

    in_first = insert_user(company_id=first["id"], email="alice@example.com")
    in_second = insert_user(company_id=second["id"], email="alice@example.com")

    assert in_first["id"] != in_second["id"]


@pytest.mark.parametrize("email", ["Alice@Example.com", "ALICE@EXAMPLE.COM"], ids=["mixed-case", "upper-case"])
def test_email_must_be_stored_normalised(db: Conn, insert_user: Insert, email: str) -> None:
    # Only the lower-case form can be stored, so "Alice@…" and "alice@…" can never both exist in one company.
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_user(email=email)

    assert excinfo.value.diag.constraint_name == "users_email_normalized_check"


@pytest.mark.parametrize(
    "email",
    ["alice", "alice@", "@example.com", "a@b@example.com", "alice smith@example.com", " alice@example.com", "", "a" * 250 + "@e.com"],
    ids=["no-at", "no-domain", "no-local-part", "two-ats", "inner-space", "leading-space", "empty", "too-long"],
)
def test_malformed_email_is_rejected(db: Conn, insert_user: Insert, email: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_user(email=email)

    assert excinfo.value.diag.constraint_name == "users_email_format_check"


@pytest.mark.parametrize("role", ["ADMIN", "staff", "SUPERUSER", ""], ids=["unknown", "lower-case", "superuser", "empty"])
def test_invalid_role_is_rejected(db: Conn, insert_user: Insert, role: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_user(role=role)

    assert excinfo.value.diag.constraint_name == "users_role_check"


def test_role_is_required(db: Conn, insert_user: Insert) -> None:
    with pytest.raises(errors.NotNullViolation) as excinfo, db.transaction():
        insert_user(role=None)

    assert excinfo.value.diag.column_name == "role"


@pytest.mark.parametrize("status", ["active", "DISABLED", ""], ids=["lower-case", "unknown", "empty"])
def test_invalid_user_status_is_rejected(db: Conn, insert_user: Insert, status: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_user(status=status)

    assert excinfo.value.diag.constraint_name == "users_status_check"


def test_every_allowed_value_in_the_python_model_is_accepted(insert_company: Insert, insert_user: Insert) -> None:
    # Together with the rejection tests above, this keeps app.models and the database constraints in step.
    companies = [insert_company(status=status.value) for status in CompanyStatus]
    users = [
        insert_user(company_id=companies[0]["id"], role=role.value, status=status.value)
        for role in UserRole
        for status in UserStatus
    ]

    assert {c["status"] for c in companies} == {s.value for s in CompanyStatus}
    assert {(u["role"], u["status"]) for u in users} == {(r.value, s.value) for r in UserRole for s in UserStatus}


@pytest.mark.security
def test_password_hash_is_stored_as_supplied_and_masked_by_the_model(db: Conn, insert_user: Insert) -> None:
    row = insert_user(password_hash=PASSWORD_HASH)

    stored = db.execute("SELECT password_hash FROM users WHERE id = %s", (row["id"],)).fetchone()
    user = User.model_validate(row)

    assert stored is not None
    assert stored["password_hash"] == PASSWORD_HASH
    assert user.password_hash.get_secret_value() == PASSWORD_HASH
    for text in (repr(user), str(user), user.model_dump_json()):
        assert PASSWORD_HASH not in text


@pytest.mark.parametrize(
    ("password_hash", "error"),
    [(None, errors.NotNullViolation), ("", errors.CheckViolation)],
    ids=["null", "empty"],
)
def test_password_hash_is_required(
    db: Conn, insert_user: Insert, password_hash: str | None, error: type[Exception]
) -> None:
    with pytest.raises(error), db.transaction():
        insert_user(password_hash=password_hash)


@pytest.mark.parametrize("full_name", [None, "", "   "], ids=["null", "empty", "blank"])
def test_full_name_is_required(db: Conn, insert_user: Insert, full_name: str | None) -> None:
    with pytest.raises((errors.NotNullViolation, errors.CheckViolation)), db.transaction():
        insert_user(full_name=full_name)


def test_deleting_a_company_with_users_is_refused(db: Conn, insert_company: Insert, insert_user: Insert) -> None:
    company = insert_company()
    user = insert_user(company_id=company["id"])
    empty_company = insert_company()

    # ON DELETE RESTRICT raises restrict_violation (SQLSTATE 23001); NO ACTION would raise foreign_key_violation.
    with pytest.raises(errors.RestrictViolation) as excinfo, db.transaction():
        db.execute("DELETE FROM companies WHERE id = %s", (company["id"],))
    deleted_empty = db.execute("DELETE FROM companies WHERE id = %s", (empty_company["id"],)).rowcount
    remaining = db.execute("SELECT company_id FROM users WHERE id = %s", (user["id"],)).fetchone()

    assert excinfo.value.diag.constraint_name == "users_company_id_fkey"
    assert remaining == {"company_id": company["id"]}  # neither orphaned nor deleted
    assert deleted_empty == 1  # a company without users can be deleted


def test_each_user_references_only_its_own_company(db: Conn, insert_company: Insert, insert_user: Insert) -> None:
    company_a = insert_company(name="Company A", slug="company-a")
    company_b = insert_company(name="Company B", slug="company-b")
    user_a = insert_user(company_id=company_a["id"], email="same@example.com")
    user_b = insert_user(company_id=company_b["id"], email="same@example.com")

    owners = db.execute(
        "SELECT u.id, c.slug FROM users u JOIN companies c ON c.id = u.company_id WHERE u.id = ANY(%s)",
        ([user_a["id"], user_b["id"]],),
    ).fetchall()
    users_of_a = db.execute("SELECT id FROM users WHERE company_id = %s", (company_a["id"],)).fetchall()
    users_of_b = db.execute("SELECT id FROM users WHERE company_id = %s", (company_b["id"],)).fetchall()

    assert {row["id"]: row["slug"] for row in owners} == {user_a["id"]: "company-a", user_b["id"]: "company-b"}
    assert [row["id"] for row in users_of_a] == [user_a["id"]]
    assert [row["id"] for row in users_of_b] == [user_b["id"]]


def test_user_updated_at_advances_on_update(db: Conn, insert_user: Insert) -> None:
    user = insert_user()

    updated = db.execute(
        "UPDATE users SET full_name = %s, updated_at = %s WHERE id = %s RETURNING created_at, updated_at",
        ("Renamed User", user["created_at"] - timedelta(days=1), user["id"]),
    ).fetchone()

    assert updated is not None
    assert updated["created_at"] == user["created_at"]
    assert updated["updated_at"] > user["updated_at"]
