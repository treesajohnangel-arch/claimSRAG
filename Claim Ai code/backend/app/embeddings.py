"""Embedding infrastructure (Phase 5): Phase 4 chunks → embedding vectors → tenant-scoped pgvector rows.

    DocumentChunks → the tenant's document record (must exist, be UPLOADED and match the chunks' provenance)
        → provider.embed_batch([chunk.text ...]) → validate_vectors (count, dimension, finite floats)
        → TenantDatabase.insert_chunk_embedding per chunk, in the caller's single transaction

- **Provider:** one local model. ``FastEmbedProvider`` runs BAAI/bge-small-en-v1.5 (ONNX, 384 dimensions) inside
  this process. Policy text never leaves the machine: the model files are read from a local cache, and loading
  never touches the network unless a download is asked for explicitly.
- **Embedding input:** exactly ``chunk.text``. No heading, metadata, ids or tenant data are added, so the same
  chunk always gives the same input.
- **Idempotent:** a chunk already embedded by the same model is left as it is and counted as ``existing``. A
  different chunking of a document that already has embeddings for this model is refused
  (``CONFLICTING_CHUNKS``), and is never stored alongside the old one.
- **Model identity:** every row records ``embedding_model``, the exact weights (``EMBEDDING_MODEL_ID``), and its
  dimension. Embeddings of another model can never pass for this one's.
- **Errors:** ``EmbeddingError`` with a reason. On any error the caller's transaction must be rolled back, which
  the API dependency and ``db.transaction()`` do; nothing is committed here.
- **Logs:** document id, model, counts and outcome only. Never chunk text, vectors or storage references.
- **No similarity search:** retrieval is Phase 6.
"""

import logging
import math
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import UUID

import psycopg
from pydantic import BaseModel, ConfigDict

from app.chunking import DocumentChunks
from app.config import BACKEND_DIR
from app.models import DocumentStatus
from app.tenant_db import TenantDatabase

logger = logging.getLogger(__name__)

# The model fastembed loads, the exact weights it maps that name to (recorded on every row), and their dimension.
# migrations/versions/0003 fixes the column at this dimension; another model needs its own migration.
FASTEMBED_MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBEDDING_MODEL_ID = "Qdrant/bge-small-en-v1.5-onnx-Q"
EMBEDDING_DIMENSION = 384
# Local, git- and docker-ignored model cache (download once with FastEmbedProvider(allow_download=True)).
MODEL_CACHE_DIR = BACKEND_DIR / ".model_cache"

_CONFLICTING_CHUNKS_KEY = "chunk_embeddings_document_chunk_index_key"


class EmbeddingFailure(StrEnum):
    EMPTY_INPUT = "EMPTY_INPUT"  # nothing to embed
    INVALID_INPUT = "INVALID_INPUT"  # a text that is not a non-blank string
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"  # the model could not be loaded (e.g. not downloaded)
    PROVIDER_FAILURE = "PROVIDER_FAILURE"  # the model failed while embedding
    VECTOR_COUNT_MISMATCH = "VECTOR_COUNT_MISMATCH"  # not exactly one vector per text
    INVALID_VECTOR = "INVALID_VECTOR"  # wrong type or dimension, NaN or infinity
    UNSUPPORTED_MODEL = "UNSUPPORTED_MODEL"  # a provider whose dimension the schema does not store
    NOT_FOUND = "NOT_FOUND"  # the tenant has no such document (also when another tenant does)
    INACTIVE = "INACTIVE"  # withdrawn documents are not embedded
    PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"  # the chunks do not come from this document record
    CONFLICTING_CHUNKS = "CONFLICTING_CHUNKS"  # the document already has a different chunking for this model


class EmbeddingError(Exception):
    """Embedding failed. ``reason`` says why; the message never contains text, vectors or tenant details."""

    def __init__(self, reason: EmbeddingFailure) -> None:
        self.reason = reason
        super().__init__(f"Embedding failed: {reason}")


class EmbeddingProvider(Protocol):
    """The provider boundary: an explicit model id and dimension, and one vector per text, in order."""

    model_id: str
    dimension: int

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...


def validate_vectors(vectors: object, count: int, dimension: int) -> list[list[float]]:
    """``vectors`` as a list of ``count`` lists of ``dimension`` finite floats, or ``EmbeddingError``."""
    if not isinstance(vectors, list) or len(vectors) != count:
        raise EmbeddingError(EmbeddingFailure.VECTOR_COUNT_MISMATCH)
    checked = []
    for vector in vectors:
        if not isinstance(vector, list) or len(vector) != dimension or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
                for value in vector):
            raise EmbeddingError(EmbeddingFailure.INVALID_VECTOR)
        checked.append([float(value) for value in vector])
    return checked


class FastEmbedProvider:
    """BAAI/bge-small-en-v1.5 through fastembed (ONNX Runtime, CPU), loaded from the local model cache."""

    model_id = EMBEDDING_MODEL_ID
    dimension = EMBEDDING_DIMENSION

    def __init__(self, cache_dir: Path = MODEL_CACHE_DIR, *, allow_download: bool = False) -> None:
        from fastembed import TextEmbedding  # imported here: only code that embeds needs the model runtime

        try:
            self._model = TextEmbedding(model_name=FASTEMBED_MODEL_NAME, cache_dir=str(cache_dir),
                                        local_files_only=not allow_download)
        except Exception as exc:  # the library fails in many ways (missing files, I/O, ONNX); report one reason
            logger.warning("embedding model unavailable model=%s error=%s", self.model_id, type(exc).__name__)
            raise EmbeddingError(EmbeddingFailure.PROVIDER_UNAVAILABLE) from None

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not texts:
            raise EmbeddingError(EmbeddingFailure.EMPTY_INPUT)
        if not all(isinstance(text, str) and text.strip() for text in texts):
            raise EmbeddingError(EmbeddingFailure.INVALID_INPUT)
        try:
            vectors = [[float(value) for value in vector] for vector in self._model.embed(texts)]
        except Exception as exc:  # inference on arbitrary text: report one reason, never the text
            logger.warning("embedding failed model=%s error=%s", self.model_id, type(exc).__name__)
            raise EmbeddingError(EmbeddingFailure.PROVIDER_FAILURE) from None
        return validate_vectors(vectors, len(texts), self.dimension)

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]


class EmbeddingResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    document_id: UUID
    embedding_model: str
    chunk_count: int
    inserted: int  # new rows
    existing: int  # chunks already embedded by this model, left unchanged


def embedding_inputs(chunks: DocumentChunks) -> list[str]:
    """The exact texts embedded: each chunk's text, unchanged, in chunk order."""
    return [chunk.text for chunk in chunks.chunks]


def embed_document_chunks(db: TenantDatabase, provider: EmbeddingProvider, chunks: DocumentChunks) -> EmbeddingResult:
    """Embed and store the chunks of one of the tenant's documents. Raises ``EmbeddingError``; the caller rolls
    back its transaction on any error, so a batch is stored whole or not at all."""
    if not isinstance(db, TenantDatabase) or not isinstance(chunks, DocumentChunks):
        raise TypeError("embed_document_chunks needs a TenantDatabase and the DocumentChunks of chunk_document")
    if getattr(provider, "dimension", None) != EMBEDDING_DIMENSION or not isinstance(
            getattr(provider, "model_id", None), str):
        raise EmbeddingError(EmbeddingFailure.UNSUPPORTED_MODEL)
    document_id, model_id = chunks.document_id, provider.model_id

    document = db.get_document(document_id)
    if document is None:
        raise _failed(document_id, model_id, EmbeddingFailure.NOT_FOUND)
    if document.status is DocumentStatus.INACTIVE:
        raise _failed(document_id, model_id, EmbeddingFailure.INACTIVE)
    if (document.policy_id, document.version, document.checksum_sha256) != (
            chunks.policy_id, chunks.document_version, chunks.checksum_sha256):
        raise _failed(document_id, model_id, EmbeddingFailure.PROVENANCE_MISMATCH)

    texts = embedding_inputs(chunks)
    vectors = validate_vectors(provider.embed_batch(texts), len(texts), EMBEDDING_DIMENSION)

    inserted = 0
    for chunk, vector in zip(chunks.chunks, vectors, strict=True):
        try:
            row = db.insert_chunk_embedding(
                policy_id=chunk.policy_id, document_id=chunk.document_id, document_version=chunk.document_version,
                chunk_id=chunk.chunk_id, chunk_index=chunk.chunk_index, page_start=chunk.page_start,
                page_end=chunk.page_end, section_heading=chunk.section_heading, chunk_text=chunk.text,
                embedding_model=model_id, embedding=vector,
            )
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == _CONFLICTING_CHUNKS_KEY:
                raise _failed(document_id, model_id, EmbeddingFailure.CONFLICTING_CHUNKS) from None
            raise
        inserted += row is not None

    result = EmbeddingResult(document_id=document_id, embedding_model=model_id, chunk_count=len(texts),
                             inserted=inserted, existing=len(texts) - inserted)
    logger.info("document embedded document_id=%s model=%s chunks=%d inserted=%d existing=%d", document_id,
                model_id, result.chunk_count, result.inserted, result.existing)
    return result


def _failed(document_id: UUID, model_id: str, reason: EmbeddingFailure) -> EmbeddingError:
    logger.warning("document embedding failed document_id=%s model=%s reason=%s", document_id, model_id, reason)
    return EmbeddingError(reason)
