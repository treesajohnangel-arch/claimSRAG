"""PDF ingestion against PostgreSQL (Phase 3): ingest_document on real tenant-scoped document records.

World: companies A and B, each with one policy and one recorded document whose two-page PDF is in a temporary
local storage. Each test runs in a rolled-back transaction; the storage root is the test's own tmp_path.
"""

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.ingestion import DocumentExtractionResult, IngestionError, IngestionFailure, ingest_document
from app.models import UserRole
from app.storage import LocalDocumentStorage
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase
from app.tokens import TokenIdentity
from tests.conftest import make_pdf, make_scanned_pdf

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
OTHER = {"A": "B", "B": "A"}


class _SpyStorage(LocalDocumentStorage):
    """The real local storage, recording every reference it is asked to read."""

    __slots__ = ("reads",)

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reads: list[str] = []

    def read(self, storage_reference: str) -> bytes:
        self.reads.append(storage_reference)
        return super().read(storage_reference)


@pytest.fixture
def storage(tmp_path: Path) -> _SpyStorage:
    return _SpyStorage(tmp_path / "documents")


@pytest.fixture
def record(insert_policy_document: Insert, storage: _SpyStorage) -> Callable[..., Row]:
    """Record a document for ``policy`` whose file holds ``data`` (stored unless ``store`` is False)."""

    def make(policy: Row, data: bytes, store: bool = True, **values: Any) -> Row:
        reference = values.pop("storage_reference", secrets.token_urlsafe(32))
        if store:
            storage.write(reference, data)
        return insert_policy_document(policy_id=policy["id"], storage_reference=reference, size_bytes=len(data),
                                      checksum_sha256=hashlib.sha256(data).hexdigest(), **values)

    return make


@pytest.fixture
def world(insert_company: Insert, insert_policy: Insert, record: Callable[..., Row]) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for company in ("A", "B"):
        rows[company] = insert_company(name=f"Company {company}", slug=f"ingest-{company.lower()}")
        rows[f"{company}-policy"] = insert_policy(company_id=rows[company]["id"], policy_number="POL-001")
        rows[f"{company}-doc"] = record(rows[f"{company}-policy"],
                                        make_pdf(f"{company} 1. Definitions", f"{company} 2. Exclusions"),
                                        original_filename=f"{company} wording.pdf", version=3)
    return rows


def _scoped(db: Conn, company: Row) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company["id"]))


def _failure(db: TenantDatabase, storage: LocalDocumentStorage, document_id: uuid.UUID) -> IngestionError:
    with pytest.raises(IngestionError) as excinfo:
        ingest_document(db, storage, document_id)
    return excinfo.value


# --- Success and provenance ------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_ingests_its_own_document_with_page_provenance(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, company: str
) -> None:
    document = world[f"{company}-doc"]

    result = ingest_document(_scoped(db, world[company]), storage, document["id"])

    assert isinstance(result, DocumentExtractionResult)
    assert (result.document_id, result.policy_id, result.document_key, result.version, result.original_filename,
            result.checksum_sha256) == (document["id"], document["policy_id"], "policy-wording", 3,
                                        f"{company} wording.pdf", document["checksum_sha256"])
    assert [(page.page_number, page.text) for page in result.pages] == [
        (1, f"{company} 1. Definitions"), (2, f"{company} 2. Exclusions")]
    assert result.page_count == 2 and result.pages_without_text == ()
    assert storage.reads == [document["storage_reference"]]  # only the record's own reference
    # No tenant internals in the result; the same input gives the same output.
    assert "company_id" not in result.model_dump() and "storage_reference" not in result.model_dump()
    assert ingest_document(_scoped(db, world[company]), storage, document["id"]) == result


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_ingestion_follows_the_verified_identitys_tenant(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, company: str
) -> None:
    # TokenIdentity (as decode_access_token builds it) → resolve_tenant → TenantDatabase: the only tenant source.
    identity = TokenIdentity(user_id=uuid.uuid4(), company_id=world[company]["id"], role=UserRole.COMPANY_ADMIN)
    tenant_db = TenantDatabase(db, resolve_tenant(identity))

    own = ingest_document(tenant_db, storage, world[f"{company}-doc"]["id"])
    foreign = _failure(tenant_db, storage, world[f"{OTHER[company]}-doc"]["id"])

    assert own.document_id == world[f"{company}-doc"]["id"]
    assert foreign.reason is IngestionFailure.NOT_FOUND
    assert storage.reads == [world[f"{company}-doc"]["storage_reference"]]


# --- Security --------------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_a_foreign_document_fails_exactly_like_an_unknown_one_and_its_file_is_never_read(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, attacker: str
) -> None:
    tenant_db = _scoped(db, world[attacker])

    foreign = _failure(tenant_db, storage, world[f"{OTHER[attacker]}-doc"]["id"])
    unknown = _failure(tenant_db, storage, uuid.uuid4())

    assert (foreign.reason, str(foreign)) == (unknown.reason, str(unknown)) == (
        IngestionFailure.NOT_FOUND, "Document ingestion failed: NOT_FOUND")
    assert storage.reads == []


@pytest.mark.security
@pytest.mark.parametrize("reference", ["../outside", "documents/../../outside", "{outside}"],
                         ids=["parent-dir", "nested-parent-dir", "absolute-path"])
def test_a_reference_that_names_a_path_cannot_redirect_ingestion(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, record: Callable[..., Row], tmp_path: Path,
    reference: str,
) -> None:
    # A planted PDF outside the storage root, matching the record's size and checksum: were it read, it would ingest.
    planted = make_pdf("PLANTED text from outside the storage root")
    outside = tmp_path / "outside"
    outside.write_bytes(planted)
    document = record(world["A-policy"], planted, store=False,
                      storage_reference=reference.format(outside=outside), version=9)

    failure = _failure(_scoped(db, world["A"]), storage, document["id"])

    assert failure.reason is IngestionFailure.SOURCE_UNAVAILABLE
    assert "PLANTED" not in str(failure) and str(outside) not in str(failure)


@pytest.mark.security
def test_a_replaced_or_corrupted_file_is_never_ingested(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, record: Callable[..., Row], tmp_path: Path
) -> None:
    original = make_pdf("Original insurer wording")
    tenant_db = _scoped(db, world["A"])
    same_size, other_size = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    tampered = bytearray(original)
    tampered[-10] ^= 0xFF
    storage.write(same_size, bytes(tampered))  # same length, different bytes
    storage.write(other_size, make_pdf("Substituted wording"))
    documents = [record(world["A-policy"], original, store=False, storage_reference=reference, version=v)
                 for v, reference in ((10, same_size), (11, other_size))]

    for document in documents:
        assert _failure(tenant_db, storage, document["id"]).reason is IngestionFailure.INTEGRITY_MISMATCH


# --- Lifecycle and source failures -----------------------------------------------------------------------------


def test_an_inactive_document_is_not_ingested(db: Conn, world: dict[str, Row], storage: _SpyStorage,
                                              record: Callable[..., Row]) -> None:
    document = record(world["A-policy"], make_pdf("Withdrawn wording"), status="INACTIVE", version=4)

    failure = _failure(_scoped(db, world["A"]), storage, document["id"])

    assert failure.reason is IngestionFailure.INACTIVE
    assert storage.reads == []


def test_a_document_without_a_stored_file_is_source_unavailable(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, record: Callable[..., Row]
) -> None:
    document = record(world["A-policy"], make_pdf("Never stored"), store=False, version=5)

    assert _failure(_scoped(db, world["A"]), storage, document["id"]).reason is IngestionFailure.SOURCE_UNAVAILABLE


@pytest.mark.parametrize(
    ("data", "reason"),
    [(make_scanned_pdf(3), IngestionFailure.NO_TEXT),
     (make_pdf(("1. Definitions\nInsured means the person named.", "2. Exclusions\nNot covered."))[:-300],
      IngestionFailure.UNREADABLE_PDF),
     (b"<html><body>Not a PDF</body></html>", IngestionFailure.UNREADABLE_PDF)],
    ids=["scanned-needs-ocr", "damaged", "not-a-pdf"],
)
def test_unusable_pdfs_fail_with_a_clear_reason(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, record: Callable[..., Row], data: bytes,
    reason: IngestionFailure,
) -> None:
    # The recorded checksum matches these bytes, so only the PDF itself is at fault.
    document = record(world["A-policy"], data, version=6)

    assert _failure(_scoped(db, world["A"]), storage, document["id"]).reason is reason


# --- Read-only and logging -------------------------------------------------------------------------------------


def test_ingestion_changes_nothing_in_the_database_or_storage(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, tmp_path: Path
) -> None:
    before = {path.name: path.read_bytes() for path in (tmp_path / "documents").iterdir()}
    tenant_db = _scoped(db, world["A"])

    ingest_document(tenant_db, storage, world["A-doc"]["id"])
    _failure(tenant_db, storage, world["B-doc"]["id"])

    assert db.execute("SELECT * FROM policy_documents WHERE id = %s", (world["A-doc"]["id"],)).fetchone() == \
        world["A-doc"]  # status (UPLOADED) and updated_at unchanged
    assert {path.name: path.read_bytes() for path in (tmp_path / "documents").iterdir()} == before


@pytest.mark.security
def test_ingestion_logs_carry_no_text_paths_or_references(
    db: Conn, world: dict[str, Row], storage: _SpyStorage, record: Callable[..., Row], tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scanned = record(world["A-policy"], make_scanned_pdf(1), version=7)
    tenant_db = _scoped(db, world["A"])

    with caplog.at_level(logging.INFO, logger="app"):
        ingest_document(tenant_db, storage, world["A-doc"]["id"])
        _failure(tenant_db, storage, scanned["id"])
        _failure(tenant_db, storage, world["B-doc"]["id"])
    logs = caplog.text

    assert f"document ingested document_id={world['A-doc']['id']} pages=2" in logs
    assert f"document_id={scanned['id']} reason=NO_TEXT" in logs
    for secret in ("Definitions", "Exclusions", str(tmp_path), world["A-doc"]["storage_reference"],
                   scanned["storage_reference"], str(world["A"]["id"]), str(world["B"]["id"])):
        assert secret not in logs
