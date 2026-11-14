"""Chat API (Phases 8-9): ``POST /chat`` answers a question from the verified tenant's own policy evidence, with
verified citations, or abstains.

    chat:use (first) → tenant → Phase 6 retrieve() → Phase 9 answer_with_citations() (the Phase 8 engine, local LLM)
        → status (ANSWERED / CONFLICT / ABSTAINED) + answer + citations + evidence

- **Permission first:** ``chat:use`` (COMPANY_ADMIN and STAFF), declared before anything else (D-021, D-023). A
  401 or 403 never loads a model, opens a connection or reaches the LLM.
- **Evidence comes only from server-side retrieval** for the token's tenant. The body is ``question``, ``top_k``
  and ``filters``, nothing else: a client cannot supply evidence, a model, a prompt or a tenant (unknown fields are
  a 422).
- **The response** carries:
  - the status and the answer;
  - citations, built by the server from the stored provenance of the passages the verified answer rests on (never
    from model or client text);
  - the evidence the model was shown.

  It has no company, vectors or storage references. An abstention has the fixed sentence and no citations; its
  internal reason is never returned.
- **Errors:** an invalid question is a 422. An unavailable, failing or timed-out model gives a generic 503. No
  prompt, evidence or provider message is ever returned.
- **Logs:** counts and latencies only.
"""

import logging
import threading
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.answering import AnswerError
from app.api.authorization import permission_required
from app.api.database import TenantDb
from app.api.search import SearchHit, search_provider
from app.embeddings import EmbeddingError, EmbeddingProvider
from app.grounding import answer_with_citations
from app.llm import LLMError, LLMProvider, OllamaProvider
from app.rbac import Permission
from app.retrieval import DEFAULT_TOP_K, MAX_QUERY_CHARS, RetrievalError, RetrievalFilters, retrieve

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

CHAT = [Depends(permission_required(Permission.CHAT_USE))]
ANSWER_UNAVAILABLE = "Answering is temporarily unavailable"
# At most this many passages per answer, so the prompt always fits the model's context (app/answering.py).
MAX_CHAT_TOP_K = 8

_provider_lock = threading.Lock()


def answer_provider(request: Request) -> LLMProvider:
    """The application's LLM provider, built from its settings on first use and then shared."""
    provider = getattr(request.app.state, "llm_provider", None)
    if provider is None:
        with _provider_lock:
            provider = getattr(request.app.state, "llm_provider", None)
            if provider is None:
                provider = OllamaProvider(request.app.state.settings)
                request.app.state.llm_provider = provider
    return provider


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: StrictStr = Field(min_length=1, max_length=MAX_QUERY_CHARS)
    top_k: int = Field(default=DEFAULT_TOP_K, strict=True, ge=1, le=MAX_CHAT_TOP_K)
    filters: RetrievalFilters = RetrievalFilters()


class CitationResponse(BaseModel):
    chunk_id: UUID
    document_id: UUID
    policy_id: UUID
    document_version: int
    page_start: int
    page_end: int
    section_heading: str | None


class ChatResponse(BaseModel):
    question: str
    status: Literal["ANSWERED", "ABSTAINED", "CONFLICT"]
    answer: str
    citations: list[CitationResponse]  # only for ANSWERED and CONFLICT; built from stored provenance
    model: str
    embedding_model: str
    generated: bool  # False when retrieval found no evidence and the model was not called
    evidence_ids: list[UUID]
    evidence: list[SearchHit]  # the retrieved passages the model was given, in order
    retrieval_ms: int
    generation_ms: int
    output_tokens: int | None


def _unavailable() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=ANSWER_UNAVAILABLE)


@router.post("/chat", dependencies=CHAT)
def chat(body: ChatRequest, embedder: Annotated[EmbeddingProvider, Depends(search_provider)],
         llm: Annotated[LLMProvider, Depends(answer_provider)], db: TenantDb) -> ChatResponse:
    started = time.monotonic()
    try:
        retrieval = retrieve(db, embedder, body.question, filters=body.filters, top_k=body.top_k)
    except RetrievalError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            detail=[{"type": "value_error", "loc": ["body", "question"], "msg": "Invalid value"}]) from None
    except EmbeddingError:
        raise _unavailable() from None
    retrieval_ms = int((time.monotonic() - started) * 1000)

    try:
        answer = answer_with_citations(llm, body.question, retrieval)
    except (LLMError, AnswerError):
        raise _unavailable() from None

    given = set(answer.evidence_ids)
    evidence = [SearchHit.model_validate(hit, from_attributes=True) for hit in retrieval.hits if hit.chunk_id in given]
    logger.info("chat answered model=%s status=%s evidence=%d citations=%d generated=%s retrieval_ms=%d "
                "generation_ms=%d total_ms=%d", answer.model, answer.status, len(evidence), len(answer.citations),
                answer.generated, retrieval_ms, answer.generation_ms, int((time.monotonic() - started) * 1000))
    return ChatResponse(question=answer.question, status=answer.status, answer=answer.answer,
                        citations=[CitationResponse.model_validate(c, from_attributes=True) for c in answer.citations],
                        model=answer.model,
                        embedding_model=retrieval.embedding_model, generated=answer.generated,
                        evidence_ids=list(answer.evidence_ids), evidence=evidence, retrieval_ms=retrieval_ms,
                        generation_ms=answer.generation_ms, output_tokens=answer.output_tokens)
