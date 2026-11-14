"""Database integrity tests for the policy_documents table (migration 0002).

The security-marked tests show the database itself keeps a document inside its policy's company: no insert, update or
shared storage reference can tie a document to another company's policy or file.
"""

import secrets
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.models import DocumentStatus

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]


def test_valid_document_is_inserted_with_generated_id_default_status_and_timestamps(
    insert_policy: Insert, insert_policy_document: Insert
) -> None:
    policy = insert_policy()
    checksum = secrets.token_hex(32)

    document = insert_policy_document(policy_id=policy["id"], document_key="policy-wording", version=1,
                                      original_filename="Family Health Cover 2026.pdf",
                                      storage_reference="tenant-x/policy-wording/v1.pdf", size_bytes=2_048_000,
                                      checksum_sha256=checksum)

    assert isinstance(document["id"], uuid.UUID) and document["id"].version == 4
    assert (document["company_id"], document["policy_id"]) == (policy["company_id"], policy["id"])
    assert (document["document_key"], document["version"], document["status"]) == ("policy-wording", 1, "UPLOADED")
    assert (document["original_filename"], document["content_type"], document["size_bytes"]) == (
        "Family Health Cover 2026.pdf", "application/pdf", 2_048_000)
    assert document["checksum_sha256"] == checksum
    assert document["created_at"].tzinfo is not None
    assert document["updated_at"] == document["created_at"]


@pytest.mark.parametrize(
    "column",
    ["company_id", "policy_id", "document_key", "version", "status", "original_filename", "storage_reference",
     "content_type", "size_bytes", "checksum_sha256"],
)
def test_required_document_column_cannot_be_null(
    db: Conn, insert_policy: Insert, insert_policy_document: Insert, column: str
) -> None:
    policy = insert_policy()
    values = {"policy_id": policy["id"], "company_id": policy["company_id"]} | {column: None}

    with pytest.raises(errors.NotNullViolation) as excinfo, db.transaction():
        insert_policy_document(**values)

    assert excinfo.value.diag.column_name == column


@pytest.mark.parametrize("status", list(DocumentStatus))
def test_every_document_status_is_accepted(insert_policy_document: Insert, status: DocumentStatus) -> None:
    assert insert_policy_document(status=status.value)["status"] == status.value


@pytest.mark.parametrize("status", ["uploaded", "ACTIVE", "SUPERSEDED", "PROCESSING", ""],
                         ids=["lower-case", "active", "superseded", "processing", "empty"])
def test_invalid_document_status_is_rejected(db: Conn, insert_policy_document: Insert, status: str) -> None:
    # ACTIVE/SUPERSEDED (Phase 11) and processing states (Phase 3) are not part of the Phase 2.1 vocabulary.
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy_document(status=status)

    assert excinfo.value.diag.constraint_name == "policy_documents_status_check"


@pytest.mark.parametrize("version", [0, -1], ids=["zero", "negative"])
def test_invalid_document_version_is_rejected(db: Conn, insert_policy_document: Insert, version: int) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy_document(version=version)

    assert excinfo.value.diag.constraint_name == "policy_documents_version_check"


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("document_key", "Policy-Wording", "policy_documents_document_key_check"),
        ("document_key", "policy wording", "policy_documents_document_key_check"),
        ("document_key", "", "policy_documents_document_key_check"),
        ("document_key", "x" * 101, "policy_documents_document_key_check"),
        ("original_filename", "", "policy_documents_original_filename_check"),
        ("original_filename", "   ", "policy_documents_original_filename_check"),
        ("original_filename", "../../etc/passwd", "policy_documents_original_filename_check"),
        ("original_filename", "C:\\docs\\policy.pdf", "policy_documents_original_filename_check"),
        ("original_filename", "policy\n.pdf", "policy_documents_original_filename_check"),
        ("original_filename", "x" * 252 + ".pdf", "policy_documents_original_filename_check"),
        ("storage_reference", "", "policy_documents_storage_reference_check"),
        ("storage_reference", " documents/a.pdf", "policy_documents_storage_reference_check"),
        ("storage_reference", "documents/a.pdf\n", "policy_documents_storage_reference_check"),
        ("storage_reference", "x" * 1025, "policy_documents_storage_reference_check"),
        ("content_type", "application/octet-stream", "policy_documents_content_type_check"),
        ("content_type", "APPLICATION/PDF", "policy_documents_content_type_check"),
        ("content_type", "text/html", "policy_documents_content_type_check"),
        ("size_bytes", 0, "policy_documents_size_bytes_check"),
        ("size_bytes", -1, "policy_documents_size_bytes_check"),
        ("checksum_sha256", "abc123", "policy_documents_checksum_sha256_check"),
        ("checksum_sha256", "A" * 64, "policy_documents_checksum_sha256_check"),
        ("checksum_sha256", "g" * 64, "policy_documents_checksum_sha256_check"),
        ("checksum_sha256", "0" * 65, "policy_documents_checksum_sha256_check"),
    ],
    ids=["key-upper-case", "key-space", "key-empty", "key-too-long", "filename-empty", "filename-blank",
         "filename-unix-path", "filename-windows-path", "filename-control-char", "filename-too-long", "storage-empty",
         "storage-leading-space", "storage-control-char", "storage-too-long", "type-octet-stream", "type-upper-case",
         "type-html", "size-zero", "size-negative", "checksum-short", "checksum-upper-case", "checksum-not-hex",
         "checksum-too-long"],
)
def test_malformed_document_metadata_is_rejected(
    db: Conn, insert_policy_document: Insert, column: str, value: object, constraint: str
) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        insert_policy_document(**{column: value})

    assert excinfo.value.diag.constraint_name == constraint


# --- Versions and uniqueness ----------------------------------------------------------------------------------


def test_a_policy_holds_several_documents_each_with_several_versions(
    insert_policy: Insert, insert_policy_document: Insert
) -> None:
    policy = insert_policy()

    rows = [insert_policy_document(policy_id=policy["id"], document_key=key, version=version)
            for key in ("policy-wording", "endorsement") for version in (1, 2, 3)]

    assert len({row["id"] for row in rows}) == 6


def test_a_document_version_cannot_be_recorded_twice(
    db: Conn, insert_policy: Insert, insert_policy_document: Insert
) -> None:
    policy = insert_policy()
    insert_policy_document(policy_id=policy["id"], document_key="policy-wording", version=2)

    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        insert_policy_document(policy_id=policy["id"], document_key="policy-wording", version=2)

    assert excinfo.value.diag.constraint_name == "policy_documents_company_id_policy_id_document_key_version_key"


def test_the_same_document_key_and_version_are_allowed_on_another_policy(
    insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert
) -> None:
    company = insert_company()
    first, second = insert_policy(company_id=company["id"]), insert_policy(company_id=company["id"])

    insert_policy_document(policy_id=first["id"], document_key="policy-wording", version=1)
    insert_policy_document(policy_id=second["id"], document_key="policy-wording", version=1)


def test_identical_files_may_be_recorded_more_than_once(
    insert_policy: Insert, insert_policy_document: Insert
) -> None:
    # The checksum is integrity metadata, not a uniqueness rule: an unchanged file re-issued as a new version, or the
    # same public document held by two companies, is legitimate. File names need not be unique either.
    checksum = secrets.token_hex(32)
    policy = insert_policy()

    insert_policy_document(policy_id=policy["id"], version=1, checksum_sha256=checksum, original_filename="p.pdf")
    insert_policy_document(policy_id=policy["id"], version=2, checksum_sha256=checksum, original_filename="p.pdf")
    insert_policy_document(checksum_sha256=checksum, original_filename="p.pdf")  # another company


# --- Same-company rule and storage references (tenant integrity) ----------------------------------------------


@pytest.mark.security
def test_a_document_cannot_reference_another_companys_policy(
    db: Conn, insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert
) -> None:
    company_a, company_b = insert_company(), insert_company()
    policy_b = insert_policy(company_id=company_b["id"])

    with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
        insert_policy_document(company_id=company_a["id"], policy_id=policy_b["id"])

    assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"


@pytest.mark.security
def test_a_document_cannot_be_moved_to_another_company_or_another_companys_policy(
    db: Conn, insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert
) -> None:
    company_a, company_b = insert_company(), insert_company()
    document = insert_policy_document(company_id=company_a["id"])
    policy_b = insert_policy(company_id=company_b["id"])

    for change, values in (("company_id = %s", (company_b["id"],)), ("policy_id = %s", (policy_b["id"],))):
        with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
            db.execute(f"UPDATE policy_documents SET {change} WHERE id = %s", (*values, document["id"]))
        assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"


@pytest.mark.security
def test_a_policy_with_documents_cannot_move_to_another_company(
    db: Conn, insert_company: Insert, insert_policy_document: Insert
) -> None:
    document = insert_policy_document()
    other = insert_company()

    with pytest.raises(errors.RestrictViolation) as excinfo, db.transaction():
        db.execute("UPDATE policies SET company_id = %s WHERE id = %s", (other["id"], document["policy_id"]))

    assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"


@pytest.mark.security
def test_a_storage_reference_belongs_to_one_document_across_all_companies(
    db: Conn, insert_policy_document: Insert
) -> None:
    # Two companies sharing one stored file would be a cross-tenant link outside the tenant filter.
    insert_policy_document(storage_reference="shared/policy.pdf")

    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        insert_policy_document(storage_reference="shared/policy.pdf")  # a new policy in a new company

    assert excinfo.value.diag.constraint_name == "policy_documents_storage_reference_key"


def test_document_must_reference_an_existing_policy(db: Conn, insert_company: Insert,
                                                   insert_policy_document: Insert) -> None:
    with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
        insert_policy_document(company_id=insert_company()["id"], policy_id=uuid.uuid4())

    assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"


# --- Delete and update behaviour ------------------------------------------------------------------------------


def test_a_policy_with_documents_cannot_be_deleted(db: Conn, insert_policy_document: Insert) -> None:
    document = insert_policy_document()

    with pytest.raises(errors.RestrictViolation) as excinfo, db.transaction():
        db.execute("DELETE FROM policies WHERE id = %s", (document["policy_id"],))

    assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"


def test_a_company_with_documents_cannot_be_deleted(db: Conn, insert_policy_document: Insert) -> None:
    document = insert_policy_document()

    with pytest.raises(errors.RestrictViolation), db.transaction():
        db.execute("DELETE FROM companies WHERE id = %s", (document["company_id"],))

    remaining = db.execute("SELECT count(*) AS n FROM policy_documents WHERE id = %s", (document["id"],)).fetchone()
    assert remaining == {"n": 1}


def test_document_updated_at_advances_on_update_and_cannot_be_set_by_the_caller(
    db: Conn, insert_policy_document: Insert
) -> None:
    document = insert_policy_document()

    updated = db.execute(
        "UPDATE policy_documents SET status = 'INACTIVE', updated_at = %s WHERE id = %s "
        "RETURNING created_at, updated_at",
        (document["created_at"] - timedelta(days=1), document["id"]),
    ).fetchone()

    assert updated is not None
    assert updated["created_at"] == document["created_at"]
    assert updated["updated_at"] > document["updated_at"]
