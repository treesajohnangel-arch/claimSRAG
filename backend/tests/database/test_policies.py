"""Database integrity tests for the policies table (migration 0002)."""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.models import InsuranceType, PolicyStatus

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]


def test_valid_policy_is_inserted_with_generated_id_default_status_and_timestamps(
    insert_company: Insert, insert_policy: Insert
) -> None:
    company = insert_company()

    policy = insert_policy(company_id=company["id"], policy_number="HLT-2026-001", name="Family Health Cover",
                           insurance_type="HEALTH")

    assert isinstance(policy["id"], uuid.UUID) and policy["id"].version == 4
    assert (policy["company_id"], policy["policy_number"], policy["name"]) == (
        company["id"], "HLT-2026-001", "Family Health Cover")
    assert (policy["insurance_type"], policy["status"]) == ("HEALTH", "ACTIVE")
    assert policy["created_at"].tzinfo is not None
    assert policy["updated_at"] == policy["created_at"]


@pytest.mark.parametrize("insurance_type", list(InsuranceType))
def test_every_supported_insurance_type_is_accepted(insert_policy: Insert, insurance_type: InsuranceType) -> None:
    assert insert_policy(insurance_type=insurance_type.value)["insurance_type"] == insurance_type.value


@pytest.mark.parametrize("insurance_type", ["health", "LIFE", "PET", ""], ids=["lower-case", "life", "pet", "empty"])
def test_unsupported_insurance_type_is_rejected(db: Conn, insert_policy: Insert, insurance_type: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy(insurance_type=insurance_type)

    assert excinfo.value.diag.constraint_name == "policies_insurance_type_check"


@pytest.mark.parametrize("status", list(PolicyStatus))
def test_every_policy_status_is_accepted(insert_policy: Insert, status: PolicyStatus) -> None:
    assert insert_policy(status=status.value)["status"] == status.value


@pytest.mark.parametrize("status", ["active", "DRAFT", "SUPERSEDED", ""], ids=["lower-case", "draft", "superseded",
                                                                              "empty"])
def test_invalid_policy_status_is_rejected(db: Conn, insert_policy: Insert, status: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy(status=status)

    assert excinfo.value.diag.constraint_name == "policies_status_check"


@pytest.mark.parametrize("column", ["company_id", "policy_number", "name", "insurance_type", "status"])
def test_required_policy_column_cannot_be_null(db: Conn, insert_policy: Insert, column: str) -> None:
    with pytest.raises(errors.NotNullViolation) as excinfo, db.transaction():
        insert_policy(**{column: None})

    assert excinfo.value.diag.column_name == column


@pytest.mark.parametrize("name", ["", "   ", "x" * 301], ids=["empty", "blank", "too-long"])
def test_blank_or_overlong_policy_name_is_rejected(db: Conn, insert_policy: Insert, name: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy(name=name)

    assert excinfo.value.diag.constraint_name == "policies_name_check"


@pytest.mark.parametrize(
    "policy_number",
    ["", "   ", " HLT-1", "HLT-1 ", "HLT\n1", "HLT\t1", "x" * 101],
    ids=["empty", "blank", "leading-space", "trailing-space", "newline", "tab", "too-long"],
)
def test_malformed_policy_number_is_rejected(db: Conn, insert_policy: Insert, policy_number: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy(policy_number=policy_number)

    assert excinfo.value.diag.constraint_name == "policies_policy_number_check"


def test_policy_number_is_unique_within_a_company(db: Conn, insert_company: Insert, insert_policy: Insert) -> None:
    company = insert_company()
    insert_policy(company_id=company["id"], policy_number="HLT-2026-001")

    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        insert_policy(company_id=company["id"], policy_number="HLT-2026-001", name="Another policy")

    assert excinfo.value.diag.constraint_name == "policies_company_id_policy_number_key"


def test_the_same_policy_number_is_allowed_in_different_companies(
    insert_company: Insert, insert_policy: Insert
) -> None:
    first = insert_policy(company_id=insert_company()["id"], policy_number="HLT-2026-001")
    second = insert_policy(company_id=insert_company()["id"], policy_number="HLT-2026-001")

    assert first["company_id"] != second["company_id"]
    assert first["policy_number"] == second["policy_number"]


def test_policy_must_reference_an_existing_company(db: Conn, insert_policy: Insert) -> None:
    with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
        insert_policy(company_id=uuid.uuid4())

    assert excinfo.value.diag.constraint_name == "policies_company_id_fkey"


def test_company_with_policies_cannot_be_deleted(db: Conn, insert_company: Insert, insert_policy: Insert) -> None:
    company = insert_company()
    insert_policy(company_id=company["id"])

    with pytest.raises(errors.RestrictViolation) as excinfo, db.transaction():
        db.execute("DELETE FROM companies WHERE id = %s", (company["id"],))

    assert excinfo.value.diag.constraint_name == "policies_company_id_fkey"


def test_policy_updated_at_advances_on_update_and_cannot_be_set_by_the_caller(db: Conn, insert_policy: Insert) -> None:
    policy = insert_policy()

    updated = db.execute(
        "UPDATE policies SET status = 'INACTIVE', updated_at = %s WHERE id = %s RETURNING created_at, updated_at",
        (policy["created_at"] - timedelta(days=1), policy["id"]),
    ).fetchone()

    assert updated is not None
    assert updated["created_at"] == policy["created_at"]
    assert updated["updated_at"] > policy["updated_at"]
