"""Unit tests for hybrid retrieval (Phase 6) without a database: query embedding, Reciprocal Rank Fusion, request
validation and the result contract. tests/database/test_retrieval.py runs the searches against PostgreSQL."""

import ast
import inspect
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import app.retrieval as retrieval_module
from app.embeddings import EMBEDDING_DIMENSION, EmbeddingError, EmbeddingFailure
from app.models import ChunkMatch
from app.retrieval import (MAX_QUERY_CHARS, RRF_K, RetrievalError, RetrievalFailure, RetrievalFilters, RetrievalHit,
                           RetrievalResult, candidates_per_path, embed_query, fuse, retrieve)


class _Provider:
    model_id, dimension = "test/fake-embedder", EMBEDDING_DIMENSION

    def __init__(self, output: Any = None, error: Exception | None = None) -> None:
        self.output, self.error, self.calls = output, error, []

    def embed_batch(self, texts: list[str]) -> Any:
        self.calls.append(texts)
        if self.error:
            raise self.error
        return self.output if self.output is not None else [[0.5] * EMBEDDING_DIMENSION for _ in texts]


def _match(name: str, score: float = 0.5) -> ChunkMatch:
    return ChunkMatch(chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name), document_id=uuid.uuid5(uuid.NAMESPACE_DNS, "doc"),
                      policy_id=uuid.uuid5(uuid.NAMESPACE_DNS, "policy"), document_version=1, chunk_index=0,
                      page_start=1, page_end=1, section_heading=None, chunk_text=f"text {name}", score=score)


# --- Query embedding -------------------------------------------------------------------------------------------


def test_the_query_is_embedded_alone_by_the_provider() -> None:
    provider = _Provider()

    vector = embed_query(provider, "  Does this policy cover emergency hospitalisation?  ")

    assert provider.calls == [["Does this policy cover emergency hospitalisation?"]]  # nothing added
    assert vector == [0.5] * EMBEDDING_DIMENSION


@pytest.mark.parametrize(("query", "reason"),
                         [("", RetrievalFailure.EMPTY_QUERY), ("   \n\t ", RetrievalFailure.EMPTY_QUERY),
                          (None, RetrievalFailure.EMPTY_QUERY), (42, RetrievalFailure.EMPTY_QUERY),
                          ("x" * (MAX_QUERY_CHARS + 1), RetrievalFailure.QUERY_TOO_LONG)],
                         ids=["empty", "whitespace", "none", "not-text", "too-long"])
def test_an_empty_or_oversized_query_is_refused_before_the_provider(query: Any, reason: RetrievalFailure) -> None:
    provider = _Provider()

    with pytest.raises(RetrievalError) as excinfo:
        embed_query(provider, query)

    assert excinfo.value.reason is reason and provider.calls == []


@pytest.mark.parametrize(
    ("provider", "reason"),
    [(_Provider(error=EmbeddingError(EmbeddingFailure.PROVIDER_FAILURE)), EmbeddingFailure.PROVIDER_FAILURE),
     (_Provider(output=[[0.5] * 383]), EmbeddingFailure.INVALID_VECTOR),
     (_Provider(output=[[float("nan")] * 384]), EmbeddingFailure.INVALID_VECTOR),
     (_Provider(output=[[float("inf")] * 384]), EmbeddingFailure.INVALID_VECTOR),
     (_Provider(output=[]), EmbeddingFailure.VECTOR_COUNT_MISMATCH)],
    ids=["provider-failure", "wrong-dimension", "nan", "infinity", "no-vector"],
)
def test_a_failed_or_invalid_query_embedding_is_a_typed_error(provider: _Provider, reason: EmbeddingFailure) -> None:
    with pytest.raises(EmbeddingError) as excinfo:
        embed_query(provider, "hospital cover")

    assert excinfo.value.reason is reason
    assert "hospital" not in str(excinfo.value)


def test_a_provider_of_another_dimension_is_refused() -> None:
    provider = _Provider()
    provider.dimension = 768

    with pytest.raises(EmbeddingError) as excinfo:
        embed_query(provider, "hospital cover")

    assert excinfo.value.reason is EmbeddingFailure.UNSUPPORTED_MODEL and provider.calls == []


def test_query_embedding_is_deterministic_for_a_deterministic_provider() -> None:
    assert embed_query(_Provider(), "co-payment") == embed_query(_Provider(), "co-payment")


# --- Reciprocal Rank Fusion ------------------------------------------------------------------------------------


def test_a_chunk_found_by_both_paths_is_deduplicated_and_scores_both_ranks() -> None:
    hits = fuse([_match("a", 0.9), _match("b", 0.8)], [_match("b", 0.4), _match("c", 0.2)], top_k=10)

    by_name = {hit.chunk_text: hit for hit in hits}
    assert [hit.chunk_text for hit in hits] == ["text b", "text a", "text c"]
    assert len(hits) == 3  # b appears once
    assert (by_name["text b"].semantic_rank, by_name["text b"].keyword_rank) == (2, 1)
    assert by_name["text b"].hybrid_score == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert (by_name["text b"].semantic_score, by_name["text b"].keyword_score) == (0.8, 0.4)


def test_semantic_only_and_keyword_only_candidates_keep_their_single_path() -> None:
    hits = fuse([_match("semantic")], [_match("keyword")], top_k=10)
    by_name = {hit.chunk_text: hit for hit in hits}

    assert (by_name["text semantic"].semantic_rank, by_name["text semantic"].keyword_rank) == (1, None)
    assert (by_name["text keyword"].semantic_rank, by_name["text keyword"].keyword_rank) == (None, 1)
    assert by_name["text semantic"].keyword_score is None and by_name["text keyword"].semantic_score is None


def test_ties_are_broken_by_chunk_id_and_order_is_deterministic() -> None:
    semantic, keyword = [_match("x"), _match("y")], [_match("y"), _match("x")]  # x and y tie exactly

    first, again = fuse(semantic, keyword, top_k=10), fuse(semantic, keyword, top_k=10)
    swapped = fuse(list(reversed(semantic)), list(reversed(keyword)), top_k=10)  # same tie, ranks swapped

    assert first[0].hybrid_score == first[1].hybrid_score
    assert [str(hit.chunk_id) for hit in first] == sorted(str(hit.chunk_id) for hit in first)
    assert first == again
    assert [hit.chunk_id for hit in swapped] == [hit.chunk_id for hit in first]


@pytest.mark.parametrize(("semantic", "keyword", "expected"),
                         [([], ["k1", "k2"], ["text k1", "text k2"]), (["s1", "s2"], [], ["text s1", "text s2"]),
                          ([], [], [])],
                         ids=["no-semantic", "no-keyword", "both-empty"])
def test_an_empty_path_leaves_the_other_ranking(semantic: list[str], keyword: list[str],
                                                expected: list[str]) -> None:
    hits = fuse([_match(n) for n in semantic], [_match(n) for n in keyword], top_k=10)

    assert [hit.chunk_text for hit in hits] == expected


def test_top_k_is_respected() -> None:
    hits = fuse([_match(f"s{n}") for n in range(30)], [_match(f"k{n}") for n in range(30)], top_k=7)

    assert len(hits) == 7


def test_candidates_per_path_is_bounded() -> None:
    assert [candidates_per_path(k) for k in (1, 5, 10, 50)] == [20, 20, 40, 200]


# --- Request validation and contract ---------------------------------------------------------------------------


@pytest.mark.parametrize("top_k", [0, -1, 51, True, "5", 2.5], ids=str)
def test_an_invalid_top_k_is_refused(top_k: Any) -> None:
    class _NoDatabase:
        pass

    with pytest.raises((RetrievalError, TypeError)):
        retrieve(_NoDatabase(), _Provider(), "hospital", top_k=top_k)  # type: ignore[arg-type]


@pytest.mark.security
@pytest.mark.parametrize("filters", [{"company_id": str(uuid.uuid4())}, {"tenant_id": str(uuid.uuid4())},
                                     {"insurance_type": "LIFE"}, {"insurance_type": "health"},
                                     {"policy_id": "not-a-uuid"}],
                         ids=["company-id", "tenant-id", "unknown-type", "lower-case-type", "bad-policy-id"])
def test_filters_accept_no_tenant_and_only_known_values(filters: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RetrievalFilters(**filters)


@pytest.mark.security
def test_the_result_contract_carries_no_company_vector_storage_or_answer() -> None:
    forbidden = {"company_id", "tenant_id", "embedding", "storage_reference", "answer", "citations"}

    assert not forbidden & set(RetrievalHit.model_fields)
    assert not forbidden & set(RetrievalResult.model_fields)
    assert set(RetrievalFilters.model_fields) == {"policy_id", "document_id", "insurance_type"}
    assert "company_id" not in ChunkMatch.model_fields


@pytest.mark.security
def test_retrieval_takes_no_tenant_input_and_holds_no_sql() -> None:
    assert list(inspect.signature(retrieve).parameters) == ["db", "provider", "query", "filters", "top_k"]
    tree = ast.parse(Path(retrieval_module.__file__).read_text(encoding="utf-8"))
    strings = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]

    assert not [s for s in strings if any(word in s.upper() for word in ("SELECT ", "FROM CHUNK", "<=>", "WHERE "))]
