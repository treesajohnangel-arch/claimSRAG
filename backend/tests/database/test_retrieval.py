"""Hybrid retrieval against PostgreSQL + pgvector (Phase 6): vector search, full-text search, filters, the interim
version rule and tenant isolation.

Embeddings are stored directly with hand-made "topic" vectors (hospital / theft / exclusions / unrelated), so the
semantic order is known exactly; tests/database/test_retrieval_quality.py uses the real model. Company B reuses A's
wording ("hospital", "insurer") on purpose. Each test runs in a rolled-back transaction.
"""

import math
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from app.embeddings import EMBEDDING_DIMENSION
from app.models import InsuranceType, UserRole
from app.retrieval import RetrievalFilters, retrieve
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase
from app.tokens import TokenIdentity

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
MODEL = "test/fake-embedder"
OTHER = {"A": "B", "B": "A"}


def topic(hospital: float = 0.0, theft: float = 0.0, exclusion: float = 0.0, unrelated: float = 0.0) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSION
    vector[0], vector[1], vector[2], vector[3] = hospital, theft, exclusion, unrelated
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


QUERIES = {"hospital expenses": topic(hospital=1.0), "vehicle theft": topic(theft=1.0),
           "motor car stolen": topic(theft=1.0),
           "cosmetic surgery exclusion": topic(exclusion=1.0), "zebra migration": topic(unrelated=1.0)}


class QueryProvider:
    """Fake provider for queries: each known query maps to its topic vector."""

    model_id, dimension = MODEL, EMBEDDING_DIMENSION

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [QUERIES[text] for text in texts]


@pytest.fixture
def world(db: Conn, insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert) -> dict[str, Row]:
    rows: dict[str, Row] = {}

    def store(name: str, company: str, document: str, index: int, text: str, vector: list[float],
              model: str = MODEL) -> None:
        doc = rows[document]
        rows[name] = TenantDatabase(db, TenantIdentity(company_id=rows[company]["id"])).insert_chunk_embedding(
            policy_id=doc["policy_id"], document_id=doc["id"], document_version=doc["version"],
            chunk_id=uuid.uuid4(), chunk_index=index, page_start=index + 1, page_end=index + 1,
            section_heading="SECTION A - COVER", chunk_text=text, embedding_model=model, embedding=vector)

    for company in ("A", "B"):
        rows[company] = insert_company(name=f"Company {company}", slug=f"retrieve-{company.lower()}")
    rows["A-health"] = insert_policy(company_id=rows["A"]["id"], policy_number="H-1", insurance_type="HEALTH")
    rows["A-motor"] = insert_policy(company_id=rows["A"]["id"], policy_number="M-1", insurance_type="MOTOR")
    rows["A-retired"] = insert_policy(company_id=rows["A"]["id"], policy_number="H-0", insurance_type="HEALTH",
                                      status="INACTIVE")
    rows["B-health"] = insert_policy(company_id=rows["B"]["id"], policy_number="H-1", insurance_type="HEALTH")
    rows["A-H"] = insert_policy_document(policy_id=rows["A-health"]["id"], version=2)
    rows["A-H-withdrawn"] = insert_policy_document(policy_id=rows["A-health"]["id"], version=1, status="INACTIVE")
    rows["A-M"] = insert_policy_document(policy_id=rows["A-motor"]["id"], version=1)
    rows["A-R"] = insert_policy_document(policy_id=rows["A-retired"]["id"], version=1)
    rows["B-H"] = insert_policy_document(policy_id=rows["B-health"]["id"], version=1)

    store("a-hospital", "A", "A-H", 0, "The insurer pays hospital expenses for in-patient treatment.", topic(hospital=1))
    store("a-cosmetic", "A", "A-H", 1, "Cosmetic surgery is excluded from cover.", topic(exclusion=1))
    store("a-ambulance", "A", "A-H", 2, "Ambulance charges are reimbursed up to the limit.",
          topic(hospital=0.8, exclusion=0.2))
    store("a-theft", "A", "A-M", 0, "Theft of the insured vehicle is covered.", topic(theft=1))
    store("a-withdrawn", "A", "A-H-withdrawn", 0, "Hospital expenses under the withdrawn wording.", topic(hospital=1))
    store("a-retired", "A", "A-R", 0, "Hospital expenses under the retired plan.", topic(hospital=1))
    store("a-other-model", "A", "A-H", 0, "The insurer pays hospital expenses for in-patient treatment.",
          topic(hospital=1), model="test/other-model")
    store("b-hospital", "B", "B-H", 0, "The insurer pays hospital expenses for B members.", topic(hospital=1))
    store("b-room", "B", "B-H", 1, "B confidential: hospital room rent is capped.", topic(hospital=0.9, exclusion=0.1))
    return rows


def _scoped(db: Conn, company: Row) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company["id"]))


def _names(world: dict[str, Row], matches: list[Any]) -> list[str]:
    by_chunk = {row.chunk_id: name for name, row in world.items() if hasattr(row, "chunk_id")}
    return [by_chunk[match.chunk_id] for match in matches]


def _vector(db: Conn, world: dict[str, Row], company: str, query: str, **filters: Any) -> list[str]:
    limit = filters.pop("limit", 20)
    return _names(world, _scoped(db, world[company]).search_chunks_by_vector(
        QUERIES[query], embedding_model=MODEL, limit=limit, **filters))


def _keywords(db: Conn, world: dict[str, Row], company: str, query: str, **filters: Any) -> list[str]:
    limit = filters.pop("limit", 20)
    return _names(world, _scoped(db, world[company]).search_chunks_by_keywords(
        query, embedding_model=MODEL, limit=limit, **filters))


# --- Vector search ---------------------------------------------------------------------------------------------


def test_vector_search_ranks_the_closest_chunks_first_with_cosine_scores(db: Conn, world: dict[str, Row]) -> None:
    matches = _scoped(db, world["A"]).search_chunks_by_vector(QUERIES["hospital expenses"], embedding_model=MODEL,
                                                              limit=20)

    assert _names(world, matches)[:2] == ["a-hospital", "a-ambulance"]
    assert matches[0].score == pytest.approx(1.0, abs=1e-6)
    assert [m.score for m in matches] == sorted((m.score for m in matches), reverse=True)
    assert matches[0].chunk_text == "The insurer pays hospital expenses for in-patient treatment."
    assert (matches[0].page_start, matches[0].section_heading) == (1, "SECTION A - COVER")


def test_vector_search_respects_the_limit(db: Conn, world: dict[str, Row]) -> None:
    assert _vector(db, world, "A", "hospital expenses", limit=1) == ["a-hospital"]


def test_vector_search_uses_only_the_given_models_embeddings(db: Conn, world: dict[str, Row]) -> None:
    names = _vector(db, world, "A", "hospital expenses")

    assert "a-other-model" not in names
    assert _scoped(db, world["A"]).search_chunks_by_vector(QUERIES["hospital expenses"],
                                                           embedding_model="test/unknown", limit=5) == []


@pytest.mark.parametrize(("filters", "expected"),
                         [({"policy_id": "A-motor"}, ["a-theft"]),
                          ({"document_id": "A-M"}, ["a-theft"]),
                          ({"insurance_type": InsuranceType.MOTOR}, ["a-theft"]),
                          ({"insurance_type": InsuranceType.TRAVEL}, [])],
                         ids=["policy", "document", "insurance-type", "no-match"])
def test_vector_search_filters_by_policy_document_and_insurance_type(
    db: Conn, world: dict[str, Row], filters: dict[str, Any], expected: list[str]
) -> None:
    resolved = {key: world[value]["id"] if key in ("policy_id", "document_id") else value
                for key, value in filters.items()}

    assert _vector(db, world, "A", "hospital expenses", **resolved) == expected


def test_withdrawn_documents_and_inactive_policies_are_never_searched(db: Conn, world: dict[str, Row]) -> None:
    # Interim rule for invariant 6 (D-029): only UPLOADED documents of ACTIVE policies are retrievable.
    for names in (_vector(db, world, "A", "hospital expenses"), _keywords(db, world, "A", "hospital expenses")):
        assert not {"a-withdrawn", "a-retired"} & set(names)


# --- Keyword search --------------------------------------------------------------------------------------------


def test_keyword_search_matches_policy_terms_and_ranks_them(db: Conn, world: dict[str, Row]) -> None:
    matches = _scoped(db, world["A"]).search_chunks_by_keywords("cosmetic surgery", embedding_model=MODEL, limit=20)

    assert _names(world, matches) == ["a-cosmetic"]
    assert matches[0].score > 0
    assert _keywords(db, world, "A", "in-patient treatment") == ["a-hospital"]
    assert _keywords(db, world, "A", "hospital") == ["a-hospital"]


@pytest.mark.parametrize("query", ["zebra migration", "the and of", "'; DROP TABLE chunk_embeddings; --",
                                   "hospital\" OR 1=1 --", "%' OR '1'='1"],
                         ids=["unrelated", "stop-words-only", "sql-injection", "quote-injection", "like-injection"])
def test_keyword_search_returns_nothing_for_unrelated_or_hostile_text(db: Conn, world: dict[str, Row],
                                                                     query: str) -> None:
    names = _keywords(db, world, "A", query)

    assert not {"b-hospital", "b-room"} & set(names)
    assert names in ([], ["a-hospital"])  # at most the genuine "hospital" match, never anything injected
    assert db.execute("SELECT count(*) AS n FROM chunk_embeddings").fetchone()["n"] == 9


def test_keyword_search_respects_limit_and_filters(db: Conn, world: dict[str, Row]) -> None:
    assert len(_keywords(db, world, "A", "covered OR cover OR excluded", limit=1)) == 1
    assert _keywords(db, world, "A", "theft", insurance_type=InsuranceType.HEALTH) == []
    assert _keywords(db, world, "A", "theft", policy_id=world["A-motor"]["id"]) == ["a-theft"]
    assert _keywords(db, world, "A", "cosmetic", document_id=world["A-M"]["id"]) == []


# --- Tenant isolation ------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_each_tenant_retrieves_only_its_own_chunks_on_both_paths(db: Conn, world: dict[str, Row], company: str) -> None:
    own_prefix = company.lower() + "-"

    for names in (_vector(db, world, company, "hospital expenses"), _keywords(db, world, company, "hospital"),
                  _keywords(db, world, company, "insurer")):
        assert names and all(name.startswith(own_prefix) for name in names), names


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
@pytest.mark.parametrize("target", ["policy", "document", "guessed-policy", "guessed-document"])
def test_a_foreign_or_guessed_filter_finds_nothing(db: Conn, world: dict[str, Row], attacker: str, target: str) -> None:
    victim = OTHER[attacker]
    ids = {"policy": ("policy_id", world[f"{victim}-health"]["id"]),
           "document": ("document_id", world[f"{victim}-H"]["id"]),
           "guessed-policy": ("policy_id", uuid.uuid4()), "guessed-document": ("document_id", uuid.uuid4())}
    key, value = ids[target]

    assert _vector(db, world, attacker, "hospital expenses", **{key: value}) == []
    assert _keywords(db, world, attacker, "hospital", **{key: value}) == []


@pytest.mark.security
def test_search_methods_take_no_company_and_refuse_untyped_filters(db: Conn, world: dict[str, Row]) -> None:
    tenant_db, b = _scoped(db, world["A"]), world["B"]["id"]

    for call in (lambda: tenant_db.search_chunks_by_vector(QUERIES["hospital expenses"], embedding_model=MODEL,
                                                           limit=5, company_id=b),
                 lambda: tenant_db.search_chunks_by_keywords("hospital", embedding_model=MODEL, limit=5,
                                                             company_id=b),
                 lambda: tenant_db.search_chunks_by_keywords("hospital", embedding_model=MODEL, limit=5,
                                                             policy_id=str(world["B-health"]["id"])),
                 lambda: tenant_db.search_chunks_by_keywords("hospital", embedding_model=MODEL, limit=5,
                                                             insurance_type="HEALTH' OR '1'='1")):
        with pytest.raises(TypeError):
            call()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        tenant_db.search_chunks_by_keywords("hospital", embedding_model=MODEL, limit=10_000)


# --- Hybrid retrieval ------------------------------------------------------------------------------------------


def test_hybrid_retrieval_merges_both_paths_without_duplicates(db: Conn, world: dict[str, Row]) -> None:
    provider = QueryProvider()

    result = retrieve(_scoped(db, world["A"]), provider, "hospital expenses", top_k=3)

    names = _names(world, list(result.hits))
    assert provider.calls == [["hospital expenses"]]  # the query alone, embedded by the stored model
    assert names[0] == "a-hospital" and len(names) == len(set(names)) == 3
    top = result.hits[0]
    assert (top.semantic_rank, top.keyword_rank) == (1, 1)  # found by both paths
    assert top.hybrid_score == max(hit.hybrid_score for hit in result.hits)
    assert (result.method, result.embedding_model, result.top_k) == ("hybrid", MODEL, 3)
    assert {hit.policy_id for hit in result.hits} <= {world["A-health"]["id"], world["A-motor"]["id"]}


def test_hybrid_retrieval_is_deterministic(db: Conn, world: dict[str, Row]) -> None:
    first = retrieve(_scoped(db, world["A"]), QueryProvider(), "hospital expenses", top_k=5)
    again = retrieve(_scoped(db, world["A"]), QueryProvider(), "hospital expenses", top_k=5)

    assert first == again


def test_hybrid_retrieval_applies_filters_on_both_paths(db: Conn, world: dict[str, Row]) -> None:
    result = retrieve(_scoped(db, world["A"]), QueryProvider(), "vehicle theft",
                      filters=RetrievalFilters(insurance_type=InsuranceType.MOTOR), top_k=5)

    assert _names(world, list(result.hits)) == ["a-theft"]
    assert result.filters.insurance_type is InsuranceType.MOTOR


def test_a_query_with_no_keyword_match_still_ranks_semantically(db: Conn, world: dict[str, Row]) -> None:
    # "motor car stolen" shares no word with any chunk, so only the vector path finds anything.
    result = retrieve(_scoped(db, world["A"]), QueryProvider(), "motor car stolen", top_k=2)

    assert _names(world, list(result.hits))[0] == "a-theft"
    assert all(hit.keyword_rank is None and hit.semantic_rank is not None for hit in result.hits)


def test_a_query_found_by_both_paths_ranks_the_shared_chunk_first(db: Conn, world: dict[str, Row]) -> None:
    # "vehicle theft" stems to 'vehicl' & 'theft', both in the theft chunk: found semantically and by keywords.
    result = retrieve(_scoped(db, world["A"]), QueryProvider(), "vehicle theft", top_k=2)

    assert _names(world, list(result.hits))[0] == "a-theft"
    assert (result.hits[0].semantic_rank, result.hits[0].keyword_rank) == (1, 1)


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_hybrid_retrieval_follows_the_verified_identitys_tenant(db: Conn, world: dict[str, Row], company: str) -> None:
    identity = TokenIdentity(user_id=uuid.uuid4(), company_id=world[company]["id"], role=UserRole.STAFF)

    result = retrieve(TenantDatabase(db, resolve_tenant(identity)), QueryProvider(), "hospital expenses", top_k=10)

    names = _names(world, list(result.hits))
    assert names and all(name.startswith(company.lower() + "-") for name in names)
    dumped = result.model_dump_json()
    for leaked in ("company_id", "storage_reference", "embedding\"", str(world[OTHER[company]]["id"])):
        assert leaked not in dumped
