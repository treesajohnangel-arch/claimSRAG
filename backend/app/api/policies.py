"""Policy and policy-document API (Phase 2.4): HTTP routes over the tenant-scoped ``TenantDatabase`` methods.

    GET    /policies                          policy:read     PATCH  /documents/{document_id}          policy:manage
    GET    /policies/{policy_id}              policy:read     PUT    /documents/{document_id}/status   policy:manage
    POST   /policies                          policy:manage   DELETE /documents/{document_id}          policy:manage
    PATCH  /policies/{policy_id}              policy:manage   GET    /policies/{policy_id}/documents   policy:read
    DELETE /policies/{policy_id}              policy:manage   POST   /policies/{policy_id}/documents   policy:manage
    GET    /documents/{document_id}           policy:read     POST   /policies/{policy_id}/documents/upload
                                                                                                       policy:manage

- **Authorization first (D-021, D-023):** each route declares its permission in ``dependencies=[...]``, so a denied
  request (401/403) never opens a database connection.
- **The tenant comes only from the verified token** (``current_tenant`` through ``TenantDb``). No route takes a
  company id. Request bodies forbid unknown fields, so a ``company_id``, ``role`` or ``storage_reference`` in a body
  is rejected (422). Ids in paths are resource ids only. Another tenant's policy or document is a 404, exactly like
  one that does not exist.
- **Responses** carry only the public fields: never ``company_id`` or ``storage_reference``.
- **Document metadata** (``POST .../documents``) records a version without a file. The server generates the opaque
  storage reference. Only the file name (PATCH) and the status (PUT .../status) of a recorded version change
  (D-025).
- **Upload** (``POST .../documents/upload``, D-035) takes the PDF itself as the raw request body
  (``Content-Type: application/pdf``) and indexes it with ``app.indexing``: stored under a server-generated
  reference, recorded, ingested, chunked and embedded in the request's tenant transaction, so it is searchable when
  the 201 arrives. The file name, document key and version are query parameters. An unreadable or text-less PDF is a
  422 naming the reason (``unreadable_pdf``, ``no_text``) before anything is stored; a body over
  ``MAX_UPLOAD_BYTES`` is a 413 (``app.request_limits``).
- **Database refusals** are mapped to safe responses by ``_refusals``. They carry no SQLSTATE, SQL, table or
  constraint name, and no value. Anything unmapped propagates to the generic 500.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
import logging
from typing import Annotated, Literal, Self
from uuid import UUID

import psycopg
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from app.api.authorization import permission_required
from app.api.database import TenantDb
from app.api.search import search_provider
from app.embeddings import EmbeddingError, EmbeddingProvider
from app.indexing import PDF_CONTENT_TYPE, index_document
from app.ingestion import IngestionError, IngestionFailure
from app.models import DocumentStatus, InsuranceType, PolicyStatus
from app.rbac import Permission
from app.storage import LocalDocumentStorage, StorageError, new_storage_reference

logger = logging.getLogger(__name__)

router = APIRouter(tags=["policies"])

READ = [Depends(permission_required(Permission.POLICY_READ))]
MANAGE = [Depends(permission_required(Permission.POLICY_MANAGE))]

POLICY_NOT_FOUND = "Policy not found"
DOCUMENT_NOT_FOUND = "Document not found"
POLICY_NUMBER_TAKEN = "A policy with this number already exists"
DOCUMENT_VERSION_TAKEN = "This document version already exists"
POLICY_HAS_DOCUMENTS = "The policy still has documents"
DOCUMENT_HAS_EMBEDDINGS = "The document still has embeddings"
INVALID_VALUE = "Invalid value"
INDEXING_UNAVAILABLE = "Indexing is temporarily unavailable"
# Why an uploaded file was refused: fixed identifiers (the 422's ``type``), never parser text.
_UPLOAD_REFUSALS = {IngestionFailure.UNREADABLE_PDF: ("unreadable_pdf", "The file is not a readable PDF"),
                    IngestionFailure.NO_TEXT: ("no_text", "The PDF has no text layer (scanned PDFs need OCR)")}

# The largest values the columns hold (integer, bigint). Larger ones get a field-level 422 here, not a database error.
_MAX_VERSION = 2**31 - 1
_MAX_SIZE_BYTES = 2**63 - 1


# --- Request and response schemas ------------------------------------------------------------------------------


class _Request(BaseModel):
    """Request bodies: unknown fields are rejected (so no company_id, role or storage_reference). Text and integers
    are strict (no 101 for "101", no "1" or true for 1); enums accept exactly their values. The database's CHECK
    constraints remain authoritative for the text rules."""

    model_config = ConfigDict(extra="forbid")


class PolicyCreate(_Request):
    policy_number: StrictStr
    name: StrictStr
    insurance_type: InsuranceType
    status: PolicyStatus = PolicyStatus.ACTIVE


class PolicyUpdate(_Request):
    """Only the fields given change. ``status`` is not changed here."""

    policy_number: StrictStr | None = None
    name: StrictStr | None = None
    insurance_type: InsuranceType | None = None

    @model_validator(mode="after")
    def _some_field_and_no_nulls(self) -> Self:
        given = self.model_dump(exclude_unset=True)
        if not given:
            raise ValueError("at least one field must be given")
        if any(value is None for value in given.values()):
            raise ValueError("fields cannot be null")
        return self


class DocumentCreate(_Request):
    """The metadata of one document version. The policy comes from the path; the storage reference is generated."""

    document_key: StrictStr
    version: int = Field(strict=True, ge=1, le=_MAX_VERSION)
    original_filename: StrictStr
    content_type: Literal["application/pdf"]
    size_bytes: int = Field(strict=True, gt=0, le=_MAX_SIZE_BYTES)
    checksum_sha256: StrictStr
    status: DocumentStatus = DocumentStatus.UPLOADED


class DocumentUpdate(_Request):
    """The only field of a recorded version that changes in place besides its status (D-025)."""

    original_filename: StrictStr


class DocumentStatusUpdate(_Request):
    status: DocumentStatus


class PolicyResponse(BaseModel):
    id: UUID
    policy_number: str
    name: str
    insurance_type: InsuranceType
    status: PolicyStatus
    created_at: datetime
    updated_at: datetime


class DocumentResponse(BaseModel):
    id: UUID
    policy_id: UUID
    document_key: str
    version: int
    original_filename: str
    content_type: str
    size_bytes: int
    checksum_sha256: str
    status: DocumentStatus
    created_at: datetime
    updated_at: datetime


def _policy(policy: object) -> PolicyResponse:
    return PolicyResponse.model_validate(policy, from_attributes=True)


def _document(document: object) -> DocumentResponse:
    return DocumentResponse.model_validate(document, from_attributes=True)


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail=detail)


# --- Database refusals → safe HTTP errors ----------------------------------------------------------------------

_CONFLICTS = {
    "policies_company_id_policy_number_key": POLICY_NUMBER_TAKEN,
    "policy_documents_company_id_policy_id_document_key_version_key": DOCUMENT_VERSION_TAKEN,
}
# CHECK constraint → the request field it judges. Only fields a client sends are listed.
_CHECKED_FIELDS = {
    "policies_policy_number_check": "policy_number",
    "policies_name_check": "name",
    "policy_documents_document_key_check": "document_key",
    "policy_documents_version_check": "version",
    "policy_documents_original_filename_check": "original_filename",
    "policy_documents_content_type_check": "content_type",
    "policy_documents_size_bytes_check": "size_bytes",
    "policy_documents_checksum_sha256_check": "checksum_sha256",
}
# The composite foreign key (company_id, policy_id) → policies (company_id, id), D-024.
_DOCUMENT_POLICY_KEY = "policy_documents_policy_fkey"
# chunk_embeddings → policy_documents (Phase 5): a document with embeddings is never deleted silently.
_EMBEDDING_DOCUMENT_KEY = "chunk_embeddings_document_fkey"


def _invalid(field: str | None) -> HTTPException:
    loc = ["body", field] if field else ["body"]
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                         detail=[{"type": "value_error", "loc": loc, "msg": INVALID_VALUE}])


@contextmanager
def _refusals() -> Iterator[None]:
    """Map the database's refusals of a write to safe responses. Each one concerns only the caller's own tenant: the
    statements are tenant-scoped, and a policy of another tenant fails exactly like a missing one."""
    try:
        yield
    except psycopg.errors.IntegrityError as exc:
        constraint = exc.diag.constraint_name
        if isinstance(exc, psycopg.errors.UniqueViolation) and constraint in _CONFLICTS:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=_CONFLICTS[constraint]) from None
        if isinstance(exc, psycopg.errors.ForeignKeyViolation) and constraint == _DOCUMENT_POLICY_KEY:
            raise _not_found(POLICY_NOT_FOUND) from None  # a document under a policy the tenant does not have
        if isinstance(exc, psycopg.errors.RestrictViolation) and constraint == _DOCUMENT_POLICY_KEY:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=POLICY_HAS_DOCUMENTS) from None
        if isinstance(exc, psycopg.errors.RestrictViolation) and constraint == _EMBEDDING_DOCUMENT_KEY:
            raise HTTPException(status.HTTP_409_CONFLICT, detail=DOCUMENT_HAS_EMBEDDINGS) from None
        if isinstance(exc, psycopg.errors.CheckViolation) and constraint in _CHECKED_FIELDS:
            raise _invalid(_CHECKED_FIELDS[constraint]) from None
        raise
    except psycopg.DataError:
        # A value PostgreSQL cannot hold, e.g. a NUL character in text (refused before it is sent).
        raise _invalid(None) from None


def _new_storage_reference() -> str:
    """An opaque, unguessable reference chosen by the server (``app.storage.new_storage_reference``). It carries no
    tenant, policy or file-name information, and no file is stored under it yet."""
    return new_storage_reference()


# --- Policies --------------------------------------------------------------------------------------------------


@router.get("/policies", dependencies=READ)
def list_policies(db: TenantDb) -> list[PolicyResponse]:
    return [_policy(policy) for policy in db.list_policies()]


@router.get("/policies/{policy_id}", dependencies=READ)
def get_policy(policy_id: UUID, db: TenantDb) -> PolicyResponse:
    policy = db.get_policy(policy_id)
    if policy is None:
        raise _not_found(POLICY_NOT_FOUND)
    return _policy(policy)


@router.post("/policies", dependencies=MANAGE, status_code=status.HTTP_201_CREATED)
def create_policy(body: PolicyCreate, db: TenantDb) -> PolicyResponse:
    with _refusals():
        return _policy(db.create_policy(policy_number=body.policy_number, name=body.name,
                                        insurance_type=body.insurance_type, status=body.status))


@router.patch("/policies/{policy_id}", dependencies=MANAGE)
def update_policy(policy_id: UUID, body: PolicyUpdate, db: TenantDb) -> PolicyResponse:
    with _refusals():
        policy = db.update_policy(policy_id, **body.model_dump(exclude_unset=True))
    if policy is None:
        raise _not_found(POLICY_NOT_FOUND)
    return _policy(policy)


@router.delete("/policies/{policy_id}", dependencies=MANAGE, status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(policy_id: UUID, db: TenantDb) -> Response:
    with _refusals():
        deleted = db.delete_policy(policy_id)
    if not deleted:
        raise _not_found(POLICY_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Policy documents ------------------------------------------------------------------------------------------


@router.get("/policies/{policy_id}/documents", dependencies=READ)
def list_documents(policy_id: UUID, db: TenantDb) -> list[DocumentResponse]:
    # An empty list means the tenant's policy has no documents; a policy the tenant does not have is a 404.
    if db.get_policy(policy_id) is None:
        raise _not_found(POLICY_NOT_FOUND)
    return [_document(document) for document in db.list_documents(policy_id)]


@router.get("/documents/{document_id}", dependencies=READ)
def get_document(document_id: UUID, db: TenantDb) -> DocumentResponse:
    document = db.get_document(document_id)
    if document is None:
        raise _not_found(DOCUMENT_NOT_FOUND)
    return _document(document)


@router.post("/policies/{policy_id}/documents", dependencies=MANAGE, status_code=status.HTTP_201_CREATED)
def create_document(policy_id: UUID, body: DocumentCreate, db: TenantDb) -> DocumentResponse:
    with _refusals():
        return _document(db.create_document(
            policy_id, document_key=body.document_key, version=body.version,
            original_filename=body.original_filename, storage_reference=_new_storage_reference(),
            content_type=body.content_type, size_bytes=body.size_bytes, checksum_sha256=body.checksum_sha256,
            status=body.status))


class DocumentUploadResponse(BaseModel):
    document: DocumentResponse
    page_count: int
    chunk_count: int


def document_storage(request: Request) -> LocalDocumentStorage:
    """The server's document storage, in the configured DOCUMENT_STORAGE_DIR. A client never chooses where."""
    return LocalDocumentStorage(request.app.state.settings.document_storage_dir)


def _refused_file(code: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                         detail=[{"type": code, "loc": ["body", "file"], "msg": message}])


@router.post("/policies/{policy_id}/documents/upload", dependencies=MANAGE, status_code=status.HTTP_201_CREATED,
             openapi_extra={"requestBody": {"required": True, "content": {PDF_CONTENT_TYPE: {
                 "schema": {"type": "string", "format": "binary"}}}}})
def upload_document(
    policy_id: UUID,
    file: Annotated[bytes, Body(media_type=PDF_CONTENT_TYPE)],
    filename: Annotated[str, Query(min_length=1, max_length=1000)],
    embedder: Annotated[EmbeddingProvider, Depends(search_provider)],
    storage: Annotated[LocalDocumentStorage, Depends(document_storage)],
    db: TenantDb,
    document_key: Annotated[str, Query(min_length=1, max_length=100)] = "policy-wording",
    version: Annotated[int, Query(ge=1, le=_MAX_VERSION)] = 1,
) -> DocumentUploadResponse:
    if db.get_policy(policy_id) is None:
        raise _not_found(POLICY_NOT_FOUND)  # another tenant's policy too
    if not file:
        raise _refused_file("empty_file", "The file is empty")
    if any(d.document_key == document_key and d.version == version for d in db.list_documents(policy_id)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail=DOCUMENT_VERSION_TAKEN)  # before anything is stored
    try:
        with _refusals():
            indexed = index_document(db, storage, embedder, policy_id=policy_id, data=file,
                                     original_filename=filename, document_key=document_key, version=version)
    except IngestionError as exc:
        refusal = _UPLOAD_REFUSALS.get(exc.reason)
        if refusal is None:
            raise
        raise _refused_file(*refusal) from None
    except (EmbeddingError, StorageError) as exc:
        logger.warning("document upload failed policy_id=%s error=%s", policy_id, type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=INDEXING_UNAVAILABLE) from None
    return DocumentUploadResponse(document=_document(indexed.document), page_count=indexed.page_count,
                                  chunk_count=indexed.chunk_count)


@router.patch("/documents/{document_id}", dependencies=MANAGE)
def update_document(document_id: UUID, body: DocumentUpdate, db: TenantDb) -> DocumentResponse:
    with _refusals():
        document = db.update_document(document_id, original_filename=body.original_filename)
    if document is None:
        raise _not_found(DOCUMENT_NOT_FOUND)
    return _document(document)


@router.put("/documents/{document_id}/status", dependencies=MANAGE)
def set_document_status(document_id: UUID, body: DocumentStatusUpdate, db: TenantDb) -> DocumentResponse:
    document = db.set_document_status(document_id, body.status)
    if document is None:
        raise _not_found(DOCUMENT_NOT_FOUND)
    return _document(document)


@router.delete("/documents/{document_id}", dependencies=MANAGE, status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: UUID, db: TenantDb) -> Response:
    with _refusals():
        deleted = db.delete_document(document_id)
    if not deleted:
        raise _not_found(DOCUMENT_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
