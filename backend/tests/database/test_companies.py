"""Database integrity tests for the companies table (migration 0001)."""

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]


def test_valid_company_is_inserted_with_generated_id_default_status_and_timestamps(insert_company: Insert) -> None:
    company = insert_company(name="Acme Insurance", slug="acme-insurance")

    assert isinstance(company["id"], uuid.UUID)
    assert company["id"].version == 4
    assert (company["name"], company["slug"], company["status"]) == ("Acme Insurance", "acme-insurance", "ACTIVE")
    assert company["created_at"].tzinfo is not None
    assert company["updated_at"] == company["created_at"]


def test_duplicate_slug_is_rejected(db: Conn, insert_company: Insert) -> None:
    insert_company(slug="acme")

    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        insert_company(name="Another Acme", slug="acme")

    assert excinfo.value.diag.constraint_name == "companies_slug_key"


def test_null_name_is_rejected(db: Conn, insert_company: Insert) -> None:
    with pytest.raises(errors.NotNullViolation) as excinfo, db.transaction():
        insert_company(name=None)

    assert excinfo.value.diag.column_name == "name"


@pytest.mark.parametrize("name", ["", "   ", "x" * 201], ids=["empty", "blank", "too-long"])
def test_empty_or_invalid_name_is_rejected(db: Conn, insert_company: Insert, name: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_company(name=name)

    assert excinfo.value.diag.constraint_name == "companies_name_check"


@pytest.mark.parametrize(
    "slug",
    ["Acme", "acme insurance", "-acme", "acme-", "acme--re", "acme_re", "", "a" * 64],
    ids=["upper-case", "space", "leading-dash", "trailing-dash", "double-dash", "underscore", "empty", "too-long"],
)
def test_malformed_slug_is_rejected(db: Conn, insert_company: Insert, slug: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_company(slug=slug)

    assert excinfo.value.diag.constraint_name == "companies_slug_check"


@pytest.mark.parametrize("status", ["active", "DELETED", ""], ids=["lower-case", "unknown", "empty"])
def test_invalid_company_status_is_rejected(db: Conn, insert_company: Insert, status: str) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_company(status=status)

    assert excinfo.value.diag.constraint_name == "companies_status_check"


def test_updated_at_advances_on_update_and_cannot_be_set_by_the_caller(db: Conn, insert_company: Insert) -> None:
    company = insert_company()

    updated = db.execute(
        "UPDATE companies SET name = %s, updated_at = %s WHERE id = %s RETURNING created_at, updated_at",
        ("Renamed Insurance", company["created_at"] - timedelta(days=1), company["id"]),
    ).fetchone()

    assert updated is not None
    assert updated["created_at"] == company["created_at"]
    assert updated["updated_at"] > company["updated_at"]
