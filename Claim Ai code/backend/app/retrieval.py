"""Hybrid retrieval (Phase 6): a question → the tenant's most relevant stored chunks, with provenance and scores.

    query ─┬─ embed_query (the Phase 5 model, the query text alone) → TenantDatabase.search_chunks_by_vector
           └─ query text → TenantDatabase.search_chunks_by_keywords (PostgreSQL full-text search)
        → both tenant-scoped and filtered in SQL (policy, document, insurance type; UPLOADED documents of ACTIVE
          policies only)
        → Reciprocal Rank Fusion: hybrid = Σ 1 / (RRF_K + rank) over the paths that found the chunk
        → hybrid score descending, then chunk_id; the first top_k

- **Evidence, not an answer:** no reranking, LLM, citations or abstention here (Phases 7–9).
- **The tenant** is the ``TenantDatabase``'s. Filters only narrow it: another tenant's policy or document id matches
  nothing.
- **Deterministic:** exact (not approximate) vector search; fixed configuration; ties broken by chunk_id.
- **Query embedding:** exactly the query text (edge whitespace trimmed), embedded by the same model that embedded
  the chunks, and searched only among that model's embeddings.
- **Logs:** counts, top_k and model only. Never the query, chunk text or vectors.
"""

import logging
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.embeddings import EMBEDDING_DIMENSION, EmbeddingError, EmbeddingFailure, EmbeddingProvider, validate_vectors
from app.models import ChunkMatch, InsuranceType
from app.tenant_db import TenantDatabase

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5
MAX_TOP_K = 50
MAX_QUERY_CHARS = 1000
# Reciprocal Rank Fusion constant (the usual 60): ranks, not raw scores, are combined, so cosine similarities and
# full-text ranks never need to be put on one scale.
RRF_K = 60


def candidates_per_path(top_k: int) -> int:
    """How many candidates each path contributes before fusion: four times top_k, at least 20, at most 200."""
    return min(max(4 * top_k, 20), 200)


class RetrievalFailure(StrEnum):
    EMPTY_QUERY = "EMPTY_QUERY"
    QUERY_TOO_LONG = "QUERY_TOO_LONG"
    INVALID_TOP_K = "INVALID_TOP_K"


class RetrievalError(Exception):
    """The retrieval request is invalid. The message never contains the query."""

    def __init__(self, reason: RetrievalFailure) -> None:
        self.reason = reason
        super().__init__(f"Retrieval failed: {reason}")


class RetrievalFilters(BaseModel):
    """Optional narrowing within the tenant. The tenant itself is never a filter: it comes from the TenantDatabase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: UUID | None = None
    document_id: UUID | None = None
    insurance_type: InsuranceType | None = None


class RetrievalHit(BaseModel):
    """One retrieved chunk: its full provenance, its text and how it was found. No company, vector or storage."""

    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    document_id: UUID
    policy_id: UUID
    document_version: int
    chunk_index: int
    page_start: int
    page_end: int
    section_heading: str | None
    chunk_text: str
    semantic_rank: int | None = Field(ge=1)  # 1 = closest; None when the vector path did not return it
    semantic_score: float | None  # cosine similarity
    keyword_rank: int | None = Field(ge=1)
    keyword_score: float | None  # PostgreSQL ts_rank_cd
    hybrid_score: float  # Reciprocal Rank Fusion score


class RetrievalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str
    filters: RetrievalFilters
    top_k: int
    method: Literal["hybrid"] = "hybrid"
    embedding_model: str
    hits: tuple[RetrievalHit, ...]


def _clean_query(query: object) -> str:
    if not isinstance(query, str) or not query.strip():
        raise RetrievalError(RetrievalFailure.EMPTY_QUERY)
    if len(query) > MAX_QUERY_CHARS:
        raise RetrievalError(RetrievalFailure.QUERY_TOO_LONG)
    return query.strip()


def embed_query(provider: EmbeddingProvider, query: str) -> list[float]:
    """The query's embedding by ``provider``: the query text alone, validated like stored embeddings."""
    if getattr(provider, "dimension", None) != EMBEDDING_DIMENSION or not isinstance(
            getattr(provider, "model_id", None), str):
        raise EmbeddingError(EmbeddingFailure.UNSUPPORTED_MODEL)
    return validate_vectors(provider.embed_batch([_clean_query(query)]), 1, EMBEDDING_DIMENSION)[0]


def fuse(semantic: list[ChunkMatch], keyword: list[ChunkMatch], top_k: int) -> tuple[RetrievalHit, ...]:
    """Merge two ranked candidate lists by Reciprocal Rank Fusion, deduplicated by chunk, in a deterministic order."""
    found: dict[UUID, dict[str, object]] = {}
    for path, matches in (("semantic", semantic), ("keyword", keyword)):
        for rank, match in enumerate(matches, start=1):
            entry = found.setdefault(match.chunk_id, {**match.model_dump(exclude={"score"}), "hybrid_score": 0.0,
                                                      "semantic_rank": None, "semantic_score": None,
                                                      "keyword_rank": None, "keyword_score": None})
            entry[f"{path}_rank"], entry[f"{path}_score"] = rank, match.score
            entry["hybrid_score"] = float(entry["hybrid_score"]) + 1.0 / (RRF_K + rank)
    ordered = sorted(found.values(), key=lambda entry: (-float(entry["hybrid_score"]), str(entry["chunk_id"])))
    return tuple(RetrievalHit(**entry) for entry in ordered[:top_k])


def retrieve(db: TenantDatabase, provider: EmbeddingProvider, query: str, *, filters: RetrievalFilters | None = None,
             top_k: int = DEFAULT_TOP_K) -> RetrievalResult:
    """The tenant's ``top_k`` most relevant chunks for ``query``, by hybrid (vector + full-text) retrieval."""
    if not isinstance(db, TenantDatabase):
        raise TypeError("retrieve needs the TenantDatabase of the verified tenant")
    filters = RetrievalFilters() if filters is None else filters
    if not isinstance(filters, RetrievalFilters):
        raise TypeError("filters must be RetrievalFilters")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or not 1 <= top_k <= MAX_TOP_K:
        raise RetrievalError(RetrievalFailure.INVALID_TOP_K)
    text = _clean_query(query)

    vector = embed_query(provider, text)
    narrowing = {"embedding_model": provider.model_id, "limit": candidates_per_path(top_k),
                 "policy_id": filters.policy_id, "document_id": filters.document_id,
                 "insurance_type": filters.insurance_type}
    semantic = db.search_chunks_by_vector(vector, **narrowing)
    keyword = db.search_chunks_by_keywords(text, **narrowing)
    hits = fuse(semantic, keyword, top_k)

    logger.info("retrieval done model=%s top_k=%d semantic=%d keyword=%d hits=%d", provider.model_id, top_k,
                len(semantic), len(keyword), len(hits))
    return RetrievalResult(query=text, filters=filters, top_k=top_k, embedding_model=provider.model_id, hits=hits)
