"""Tenant-scoped policy-document writes against the real schema (Phase 2.3).

Company A owns policies A1 (versions 1 and 2 of its "policy-wording") and A2 (no documents). Company B owns B1,
whose "policy-wording" version 1 deliberately repeats A1's key and version (and B1 repeats A1's policy number). Each
test runs in a transaction that is rolled back, in the throwaway test database. A statement expected to fail runs
inside ``db.transaction()`` (a savepoint).
"""

import secrets
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import errors
from pydantic import ValidationError

from app.models import DocumentStatus, PolicyDocument, PolicyStatus, UserRole
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase, TenantScopeError
from app.tokens import TokenIdentity

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
OTHER = {"A": "B", "B": "A"}
DOCUMENTS = ("A1-v1", "A1-v2", "B1-v1")


@pytest.fixture
def tenants(insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert) -> dict[str, Row]:
    a = insert_company(name="Company A", slug="documents-tenant-a")
    b = insert_company(name="Company B", slug="documents-tenant-b")
    rows = {"A": a, "B": b,
            "A1": insert_policy(company_id=a["id"], policy_number="POL-001"),
            "A2": insert_policy(company_id=a["id"], policy_number="POL-002"),
            "B1": insert_policy(company_id=b["id"], policy_number="POL-001")}
    rows["A1-v1"] = insert_policy_document(policy_id=rows["A1"]["id"], document_key="policy-wording", version=1)
    rows["A1-v2"] = insert_policy_document(policy_id=rows["A1"]["id"], document_key="policy-wording", version=2)
    rows["B1-v1"] = insert_policy_document(policy_id=rows["B1"]["id"], document_key="policy-wording", version=1)
    return rows


def _scoped(db: Conn, company: Row) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company["id"]))


def _metadata(**changes: Any) -> dict[str, Any]:
    """Valid metadata for a new document version (a fresh storage reference each time); override any field."""
    return {"document_key": "endorsement", "version": 1, "original_filename": "Endorsement 2026.pdf",
            "storage_reference": f"test-documents/{secrets.token_hex(8)}.pdf", "content_type": "application/pdf",
            "size_bytes": 52_000, "checksum_sha256": secrets.token_hex(32), **changes}


def _row(db: Conn, document_id: uuid.UUID) -> Row | None:
    return db.execute("SELECT * FROM policy_documents WHERE id = %s", (document_id,)).fetchone()


def _policy_row(db: Conn, policy_id: uuid.UUID) -> Row | None:
    return db.execute("SELECT * FROM policies WHERE id = %s", (policy_id,)).fetchone()


def _count(db: Conn, company: Row) -> int:
    return db.execute("SELECT count(*) AS n FROM policy_documents WHERE company_id = %s",
                      (company["id"],)).fetchone()["n"]


# --- Create ---------------------------------------------------------------------------------------------------


def test_create_document_records_the_metadata_under_the_tenants_policy(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])
    metadata = _metadata()

    created = tenant_a.create_document(tenants["A2"]["id"], **metadata)

    stored = _row(db, created.id)
    assert isinstance(created, PolicyDocument) and created.id.version == 4
    assert (created.company_id, created.policy_id) == (tenants["A"]["id"], tenants["A2"]["id"])
    assert (stored["company_id"], stored["policy_id"]) == (tenants["A"]["id"], tenants["A2"]["id"])
    assert {field: getattr(created, field) for field in metadata} == metadata
    assert {field: stored[field] for field in metadata} == metadata
    assert created.status is DocumentStatus.UPLOADED  # the database default
    assert created.updated_at == created.created_at
    assert tenant_a.get_document(created.id) == created
    assert tenant_a.list_documents(tenants["A2"]["id"]) == [created]


def test_a_document_can_be_recorded_as_inactive(db: Conn, tenants: dict[str, Row]) -> None:
    created = _scoped(db, tenants["A"]).create_document(tenants["A2"]["id"], **_metadata(),
                                                        status=DocumentStatus.INACTIVE)

    assert created.status is DocumentStatus.INACTIVE


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("document_key", "Policy-Wording", "policy_documents_document_key_check"),
        ("document_key", "policy wording", "policy_documents_document_key_check"),
        ("document_key", "", "policy_documents_document_key_check"),
        ("version", 0, "policy_documents_version_check"),
        ("version", -1, "policy_documents_version_check"),
        ("original_filename", "../policy.pdf", "policy_documents_original_filename_check"),
        ("original_filename", "C:\\docs\\policy.pdf", "policy_documents_original_filename_check"),
        ("original_filename", "policy\r.pdf", "policy_documents_original_filename_check"),
        ("original_filename", "   ", "policy_documents_original_filename_check"),
        ("storage_reference", "", "policy_documents_storage_reference_check"),
        ("storage_reference", "documents/a.pdf ", "policy_documents_storage_reference_check"),
        ("content_type", "application/octet-stream", "policy_documents_content_type_check"),
        ("content_type", "text/html", "policy_documents_content_type_check"),
        ("size_bytes", 0, "policy_documents_size_bytes_check"),
        ("size_bytes", -5, "policy_documents_size_bytes_check"),
        ("checksum_sha256", "A" * 64, "policy_documents_checksum_sha256_check"),
        ("checksum_sha256", "abc", "policy_documents_checksum_sha256_check"),
        ("checksum_sha256", "z" * 64, "policy_documents_checksum_sha256_check"),
    ],
    ids=["key-upper-case", "key-space", "key-empty", "version-zero", "version-negative", "filename-unix-path",
         "filename-windows-path", "filename-control-char", "filename-blank", "storage-empty", "storage-padded",
         "type-octet-stream", "type-html", "size-zero", "size-negative", "checksum-upper-case", "checksum-short",
         "checksum-not-hex"],
)
def test_invalid_metadata_is_rejected_by_the_database(
    db: Conn, tenants: dict[str, Row], field: str, value: object, constraint: str
) -> None:
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        _scoped(db, tenants["A"]).create_document(tenants["A2"]["id"], **_metadata(**{field: value}))

    assert excinfo.value.diag.constraint_name == constraint
    assert _count(db, tenants["A"]) == 2


# --- Versioning -----------------------------------------------------------------------------------------------


def test_the_same_version_of_a_document_cannot_be_recorded_twice(db: Conn, tenants: dict[str, Row]) -> None:
    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        _scoped(db, tenants["A"]).create_document(tenants["A1"]["id"],
                                                  **_metadata(document_key="policy-wording", version=2))

    assert excinfo.value.diag.constraint_name == "policy_documents_company_id_policy_id_document_key_version_key"
    assert _row(db, tenants["A1-v2"]["id"]) == tenants["A1-v2"]
    assert _count(db, tenants["A"]) == 2


@pytest.mark.parametrize(
    ("policy", "document_key", "version"),
    [("A1", "policy-wording", 3), ("A1", "endorsement", 1), ("A2", "policy-wording", 1)],
    ids=["same-key-next-version", "same-version-other-key", "same-key-and-version-other-policy"],
)
def test_keys_versions_and_policies_combine_freely_within_the_unique_key(
    db: Conn, tenants: dict[str, Row], policy: str, document_key: str, version: int
) -> None:
    created = _scoped(db, tenants["A"]).create_document(tenants[policy]["id"],
                                                        **_metadata(document_key=document_key, version=version))

    assert (created.policy_id, created.document_key, created.version) == (tenants[policy]["id"], document_key, version)
    assert _count(db, tenants["A"]) == 3


def test_the_same_key_and_version_are_allowed_in_another_company(db: Conn, tenants: dict[str, Row]) -> None:
    # The fixture already holds "policy-wording" v1 in both companies; each can add the same next version too.
    a = _scoped(db, tenants["A"]).create_document(tenants["A1"]["id"],
                                                  **_metadata(document_key="policy-wording", version=3))
    b = _scoped(db, tenants["B"]).create_document(tenants["B1"]["id"],
                                                  **_metadata(document_key="policy-wording", version=3))

    assert (a.document_key, a.version) == (b.document_key, b.version)
    assert (a.company_id, b.company_id) == (tenants["A"]["id"], tenants["B"]["id"])


def test_versions_are_stored_as_given_and_earlier_versions_are_untouched(db: Conn, tenants: dict[str, Row]) -> None:
    # No automatic numbering, current-version selection or superseding (Phase 11): a new version is only a new row.
    # An identical file (same checksum) is a legitimate new version.
    tenant_a = _scoped(db, tenants["A"])

    created = tenant_a.create_document(tenants["A1"]["id"], **_metadata(
        document_key="policy-wording", version=7, checksum_sha256=tenants["A1-v2"]["checksum_sha256"]))

    assert created.version == 7
    assert [(d.version, d.status) for d in tenant_a.list_documents(tenants["A1"]["id"])] == [
        (1, DocumentStatus.UPLOADED), (2, DocumentStatus.UPLOADED), (7, DocumentStatus.UPLOADED)]
    assert (_row(db, tenants["A1-v1"]["id"]), _row(db, tenants["A1-v2"]["id"])) == (tenants["A1-v1"],
                                                                                  tenants["A1-v2"])


# --- Update and status ----------------------------------------------------------------------------------------


def test_update_changes_only_the_file_name_and_advances_updated_at(db: Conn, tenants: dict[str, Row]) -> None:
    before = tenants["A1-v1"]

    updated = _scoped(db, tenants["A"]).update_document(before["id"], original_filename="Policy Wording (signed).pdf")

    after = _row(db, before["id"])
    assert updated is not None and updated.original_filename == after["original_filename"]
    assert after["original_filename"] == "Policy Wording (signed).pdf"
    unchanged = set(before) - {"original_filename", "updated_at"}
    assert {column: after[column] for column in unchanged} == {column: before[column] for column in unchanged}
    assert after["updated_at"] > before["updated_at"]


def test_update_keeps_the_file_name_rule(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        tenant_a.update_document(tenants["A1-v1"]["id"], original_filename="../../etc/passwd")
    assert excinfo.value.diag.constraint_name == "policy_documents_original_filename_check"
    with pytest.raises(TypeError):
        tenant_a.update_document(tenants["A1-v1"]["id"], original_filename=None)  # type: ignore[arg-type]
    assert _row(db, tenants["A1-v1"]["id"]) == tenants["A1-v1"]


def test_status_changes_between_uploaded_and_inactive_without_touching_other_versions(
    db: Conn, tenants: dict[str, Row]
) -> None:
    tenant_a = _scoped(db, tenants["A"])
    before = tenants["A1-v1"]

    inactive = tenant_a.set_document_status(before["id"], DocumentStatus.INACTIVE)
    uploaded = tenant_a.set_document_status(before["id"], DocumentStatus.UPLOADED)

    assert inactive is not None and uploaded is not None
    assert (inactive.status, uploaded.status) == (DocumentStatus.INACTIVE, DocumentStatus.UPLOADED)
    assert (uploaded.original_filename, uploaded.storage_reference, uploaded.checksum_sha256) == (
        before["original_filename"], before["storage_reference"], before["checksum_sha256"])
    assert before["updated_at"] < inactive.updated_at < uploaded.updated_at
    assert _row(db, tenants["A1-v2"]["id"]) == tenants["A1-v2"]  # no superseding of other versions


@pytest.mark.parametrize("status", ["INACTIVE", "ACTIVE", "SUPERSEDED", PolicyStatus.INACTIVE],
                         ids=["text", "active", "superseded", "policy-status"])
def test_only_document_statuses_are_accepted(db: Conn, tenants: dict[str, Row], status: object) -> None:
    # ACTIVE stays out of the document vocabulary: it means a validated current version (Phase 11, D-024).
    with pytest.raises(TypeError):
        _scoped(db, tenants["A"]).set_document_status(tenants["A1-v1"]["id"], status)  # type: ignore[arg-type]

    assert _row(db, tenants["A1-v1"]["id"]) == tenants["A1-v1"]


def test_updating_or_re_statusing_a_missing_document_returns_none(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    assert tenant_a.update_document(uuid.uuid4(), original_filename="ghost.pdf") is None
    assert tenant_a.set_document_status(uuid.uuid4(), DocumentStatus.INACTIVE) is None


# --- Delete ---------------------------------------------------------------------------------------------------


def test_a_document_is_deleted_without_touching_its_policy_or_other_versions(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    assert tenant_a.delete_document(tenants["A1-v1"]["id"]) is True

    assert _row(db, tenants["A1-v1"]["id"]) is None
    assert tenant_a.get_document(tenants["A1-v1"]["id"]) is None
    assert _policy_row(db, tenants["A1"]["id"]) == tenants["A1"]
    assert _row(db, tenants["A1-v2"]["id"]) == tenants["A1-v2"]
    assert tenant_a.delete_document(tenants["A1-v1"]["id"]) is False  # already gone


def test_deleting_a_missing_document_returns_false(db: Conn, tenants: dict[str, Row]) -> None:
    assert _scoped(db, tenants["A"]).delete_document(uuid.uuid4()) is False
    assert _count(db, tenants["A"]) == 2


def test_a_policy_can_be_deleted_only_after_its_documents_are(db: Conn, tenants: dict[str, Row]) -> None:
    # Nothing cascades: each document record is deleted explicitly, and only then may the policy go.
    tenant_a = _scoped(db, tenants["A"])

    with pytest.raises(errors.RestrictViolation), db.transaction():
        tenant_a.delete_policy(tenants["A1"]["id"])
    assert all(tenant_a.delete_document(tenants[name]["id"]) for name in ("A1-v1", "A1-v2"))

    assert tenant_a.delete_policy(tenants["A1"]["id"]) is True
    assert _row(db, tenants["B1-v1"]["id"]) == tenants["B1-v1"]


# --- Security: the document write boundary --------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_creates_documents_only_under_its_own_policies(
    db: Conn, tenants: dict[str, Row], company: str
) -> None:
    tenant = _scoped(db, tenants[company])
    own, foreign = tenants[f"{company}1"], tenants[f"{OTHER[company]}1"]
    before = {name: _count(db, tenants[name]) for name in ("A", "B")}

    created = tenant.create_document(own["id"], **_metadata())
    with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
        tenant.create_document(foreign["id"], **_metadata())

    assert (created.company_id, created.policy_id) == (tenants[company]["id"], own["id"])
    assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"  # the composite same-company key
    assert _count(db, tenants[company]) == before[company] + 1
    assert _count(db, tenants[OTHER[company]]) == before[OTHER[company]]


@pytest.mark.security
def test_a_foreign_policy_id_fails_exactly_like_a_missing_one(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    def failure(policy_id: uuid.UUID) -> tuple[Any, ...]:
        with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
            tenant_a.create_document(policy_id, **_metadata(storage_reference="test-documents/probe.pdf"))
        diag = excinfo.value.diag
        detail = (diag.message_detail or "").replace(str(policy_id), "<policy>")
        return excinfo.value.sqlstate, diag.constraint_name, diag.table_name, diag.message_primary, detail

    foreign, missing = failure(tenants["B1"]["id"]), failure(uuid.uuid4())

    assert foreign == missing
    # The error names only what the caller already has: its own company and the id it sent.
    assert str(tenants["B"]["id"]) not in foreign[-1] and str(tenants["A"]["id"]) in foreign[-1]


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_another_tenants_document_cannot_be_updated_re_statused_or_deleted(
    db: Conn, tenants: dict[str, Row], company: str
) -> None:
    attacker = _scoped(db, tenants[company])

    for name in (name for name in DOCUMENTS if not name.startswith(company)):
        document_id = tenants[name]["id"]
        assert attacker.update_document(document_id, original_filename="hijacked.pdf") is None
        assert attacker.set_document_status(document_id, DocumentStatus.INACTIVE) is None
        assert attacker.delete_document(document_id) is False
        assert _row(db, document_id) == tenants[name]  # every column, updated_at included, untouched


@pytest.mark.security
def test_a_guessed_foreign_document_id_is_indistinguishable_from_a_missing_one(
    db: Conn, tenants: dict[str, Row]
) -> None:
    attacker = _scoped(db, tenants["A"])

    def outcomes(document_id: uuid.UUID) -> tuple[Any, ...]:
        return (attacker.get_document(document_id), attacker.update_document(document_id, original_filename="x.pdf"),
                attacker.set_document_status(document_id, DocumentStatus.UPLOADED),
                attacker.delete_document(document_id))

    assert outcomes(tenants["B1-v1"]["id"]) == outcomes(uuid.uuid4()) == (None, None, None, False)


@pytest.mark.security
def test_a_document_cannot_be_moved_to_another_policy_or_tenant_or_get_a_new_id(
    db: Conn, tenants: dict[str, Row]
) -> None:
    tenant_a, document = _scoped(db, tenants["A"]), tenants["A1-v1"]
    changes = {"policy_id": tenants["B1"]["id"], "company_id": tenants["B"]["id"], "id": tenants["B1-v1"]["id"]}

    for field, value in [*changes.items(), ("policy_id", tenants["A2"]["id"])]:  # even within the tenant
        with pytest.raises(TypeError):
            tenant_a.update_document(document["id"], original_filename="moved.pdf", **{field: value})

    assert _row(db, document["id"]) == document
    assert (_count(db, tenants["A"]), _count(db, tenants["B"])) == (2, 1)


@pytest.mark.security
def test_a_caller_cannot_supply_a_company_id(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a, b = _scoped(db, tenants["A"]), tenants["B"]["id"]

    with pytest.raises(TypeError):
        tenant_a.create_document(tenants["B1"]["id"], **_metadata(), company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.update_document(tenants["B1-v1"]["id"], original_filename="x.pdf",
                                 company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.set_document_status(tenants["B1-v1"]["id"], DocumentStatus.INACTIVE,
                                     company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.delete_document(tenants["B1-v1"]["id"], company_id=b)  # type: ignore[call-arg]

    assert _row(db, tenants["B1-v1"]["id"]) == tenants["B1-v1"]
    assert (_count(db, tenants["A"]), _count(db, tenants["B"])) == (2, 1)


@pytest.mark.security
def test_an_altered_or_invalid_tenant_identity_cannot_write_documents(db: Conn, tenants: dict[str, Row]) -> None:
    b = tenants["B"]["id"]
    for invalid in (uuid.UUID(int=0), "not-a-uuid", None):
        with pytest.raises(ValidationError):
            TenantIdentity(company_id=invalid)  # type: ignore[arg-type]
    for forged in (SimpleNamespace(company_id=b), {"company_id": b}, TenantIdentity.model_construct(company_id=str(b)),
                   TenantIdentity.model_construct(company_id=uuid.UUID(int=0)), None):
        with pytest.raises(TenantScopeError):
            TenantDatabase(db, forged)  # type: ignore[arg-type]

    tenant = TenantIdentity(company_id=tenants["A"]["id"])
    tenant_a = TenantDatabase(db, tenant)
    with pytest.raises(ValidationError):
        tenant.company_id = b  # type: ignore[misc]  # frozen
    with pytest.raises(AttributeError):
        tenant_a._tenant = TenantIdentity(company_id=b)  # type: ignore[misc]  # immutable
    with pytest.raises(errors.ForeignKeyViolation), db.transaction():
        tenant_a.create_document(tenants["B1"]["id"], **_metadata())  # still tenant A: B's policy stays out of reach

    created = tenant_a.create_document(tenants["A2"]["id"], **_metadata())
    assert created.company_id == tenants["A"]["id"]
    assert _count(db, tenants["B"]) == 1


@pytest.mark.security
def test_a_storage_reference_cannot_be_shared_with_another_tenant(db: Conn, tenants: dict[str, Row]) -> None:
    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        _scoped(db, tenants["B"]).create_document(
            tenants["B1"]["id"], **_metadata(storage_reference=tenants["A1-v1"]["storage_reference"]))

    assert excinfo.value.diag.constraint_name == "policy_documents_storage_reference_key"
    assert _count(db, tenants["B"]) == 1


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_document_writes_follow_the_verified_identitys_tenant(db: Conn, tenants: dict[str, Row], company: str) -> None:
    # TokenIdentity (as decode_access_token builds it) → resolve_tenant → TenantDatabase: the only tenant source.
    identity = TokenIdentity(user_id=uuid.uuid4(), company_id=tenants[company]["id"], role=UserRole.COMPANY_ADMIN)
    tenant_db = TenantDatabase(db, resolve_tenant(identity))

    created = tenant_db.create_document(tenants[f"{company}1"]["id"], **_metadata())
    with pytest.raises(errors.ForeignKeyViolation), db.transaction():
        tenant_db.create_document(tenants[f"{OTHER[company]}1"]["id"], **_metadata())

    assert created.company_id == tenants[company]["id"]
    assert tenant_db.delete_document(tenants[f"{OTHER[company]}1-v1"]["id"]) is False
