"""Search API (Phase 6): ``POST /search`` runs hybrid retrieval over the verified tenant's stored chunks.

- **Permission first:** ``search:use`` (COMPANY_ADMIN and STAFF), declared before anything else (D-021, D-023). A
  401 or 403 never loads the model or opens a connection.
- **The tenant** comes only from the token, through ``TenantDb``. The body may narrow the search by policy,
  document or insurance type; another tenant's ids simply match nothing. Unknown fields such as ``company_id``
  are a 422.
- **Responses** carry chunk provenance, text and retrieval scores. They never include the company, vectors or
  storage references, and they contain no answer: generation is Phase 8.
- **The model** (Phase 5's local provider) is loaded once per application, on first use. If it is unavailable,
  the response is a generic 503.
"""

import threading
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.api.authorization import permission_required
from app.api.database import TenantDb
from app.embeddings import EmbeddingError, EmbeddingProvider, FastEmbedProvider
from app.rbac import Permission
from app.retrieval import DEFAULT_TOP_K, MAX_QUERY_CHARS, MAX_TOP_K, RetrievalError, RetrievalFilters, retrieve

router = APIRouter(tags=["search"])

SEARCH = [Depends(permission_required(Permission.SEARCH_USE))]
SEARCH_UNAVAILABLE = "Search is temporarily unavailable"

_provider_lock = threading.Lock()


def search_provider(request: Request) -> EmbeddingProvider:
    """The application's embedding provider, loaded on first use and then shared."""
    provider = getattr(request.app.state, "embedding_provider", None)
    if provider is None:
        with _provider_lock:
            provider = getattr(request.app.state, "embedding_provider", None)
            if provider is None:
                try:
                    provider = FastEmbedProvider()
                except EmbeddingError:
                    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from None
                request.app.state.embedding_provider = provider
    return provider


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: StrictStr = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_k: int = Field(default=DEFAULT_TOP_K, strict=True, ge=1, le=MAX_TOP_K)
    filters: RetrievalFilters = RetrievalFilters()


class SearchHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    policy_id: UUID
    document_version: int
    chunk_index: int
    page_start: int
    page_end: int
    section_heading: str | None
    chunk_text: str
    semantic_rank: int | None
    semantic_score: float | None
    keyword_rank: int | None
    keyword_score: float | None
    hybrid_score: float


class SearchResponse(BaseModel):
    query: str
    filters: RetrievalFilters
    top_k: int
    method: Literal["hybrid"]
    embedding_model: str
    hits: list[SearchHit]


@router.post("/search", dependencies=SEARCH)
def search(body: SearchRequest, provider: Annotated[EmbeddingProvider, Depends(search_provider)],
           db: TenantDb) -> SearchResponse:
    try:
        result = retrieve(db, provider, body.query, filters=body.filters, top_k=body.top_k)
    except RetrievalError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=[{"type": "value_error", "loc": ["body", "query"], "msg": "Invalid value"}]) from None
    except EmbeddingError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=SEARCH_UNAVAILABLE) from None
    return SearchResponse.model_validate(result, from_attributes=True)
