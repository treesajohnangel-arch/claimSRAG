"""Indexing a policy PDF (D-035): the existing services, in order, for one uploaded file.

    PDF bytes → extract_pages (Phase 3; refuse anything unreadable before storing it)
      → LocalDocumentStorage.write under a new server-generated reference
      → TenantDatabase.create_document (size and SHA-256 of these bytes)
      → ingest_document (Phase 3: re-reads the stored file and checks it against the record)
      → chunk_document (Phase 4) → embed_document_chunks (Phase 5) → searchable (Phase 6)

Used by the upload route (``app.api.policies``) and the operator tooling (``scripts/workspace.py``), so both index a
document the same way.

- **In the caller's tenant and transaction.** Everything is written through the caller's ``TenantDatabase``, so a
  policy of another tenant fails exactly like a missing one (``ForeignKeyViolation``), and nothing commits here. A
  failure after the file is stored rolls the records back; the stored file stays, unreferenced (storage is
  write-once).
- **The client controls no path.** The file is named by ``new_storage_reference()``; the client's file name is kept
  only as display metadata, reduced to a plain name (``safe_filename``).
- **Page provenance is kept:** chunks carry their pages and sections from the extraction (D-027).
- **Errors:** ``IngestionError`` (``UNREADABLE_PDF``, ``NO_TEXT``), ``StorageError``, ``EmbeddingError`` and database
  errors propagate, for the caller to map. No message contains text or paths.
"""

import hashlib
import logging
import re
import unicodedata
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.chunking import chunk_document
from app.embeddings import EmbeddingProvider, embed_document_chunks
from app.ingestion import extract_pages, ingest_document
from app.models import PolicyDocument
from app.storage import LocalDocumentStorage, new_storage_reference
from app.tenant_db import TenantDatabase

logger = logging.getLogger(__name__)

PDF_CONTENT_TYPE = "application/pdf"
MAX_FILENAME_CHARS = 255  # policy_documents_original_filename_check
DEFAULT_FILENAME = "policy-document.pdf"


class IndexedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: PolicyDocument
    page_count: int
    chunk_count: int
    embedded: int


def safe_filename(name: str) -> str:
    """A display name for an uploaded file: the last path component only, without control or format characters,
    trimmed to the stored limit with its extension kept. Never used to build a path."""
    base = re.split(r"[\\/]", name or "")[-1]
    cleaned = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C").strip()
    if not cleaned or cleaned in {".", ".."}:
        return DEFAULT_FILENAME
    if len(cleaned) > MAX_FILENAME_CHARS:
        stem, dot, extension = cleaned.rpartition(".")
        if dot and 0 < len(extension) <= 10:
            cleaned = f"{stem[:MAX_FILENAME_CHARS - len(extension) - 1].rstrip()}.{extension}"
        else:
            cleaned = cleaned[:MAX_FILENAME_CHARS].rstrip()
    return cleaned


def index_document(db: TenantDatabase, storage: LocalDocumentStorage, embedder: EmbeddingProvider, *,
                   policy_id: UUID, data: bytes, original_filename: str, document_key: str,
                   version: int) -> IndexedDocument:
    """Store, record, ingest, chunk and embed one PDF as a version of the tenant's policy."""
    if not isinstance(db, TenantDatabase) or not isinstance(data, bytes):
        raise TypeError("index_document needs a TenantDatabase and the file's bytes")
    extract_pages(data)  # unreadable, encrypted, damaged or text-less files are refused before anything is stored
    reference = new_storage_reference()
    storage.write(reference, data)
    document = db.create_document(policy_id, document_key=document_key, version=version,
                                  original_filename=safe_filename(original_filename), storage_reference=reference,
                                  content_type=PDF_CONTENT_TYPE, size_bytes=len(data),
                                  checksum_sha256=hashlib.sha256(data).hexdigest())
    extraction = ingest_document(db, storage, document.id)
    chunks = chunk_document(extraction)
    result = embed_document_chunks(db, embedder, chunks)
    logger.info("document indexed document_id=%s pages=%d chunks=%d embedded=%d", document.id,
                extraction.page_count, result.chunk_count, result.inserted)
    return IndexedDocument(document=document, page_count=extraction.page_count, chunk_count=result.chunk_count,
                           embedded=result.inserted)
