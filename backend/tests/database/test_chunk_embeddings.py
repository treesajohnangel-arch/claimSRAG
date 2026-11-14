"""Chunk embeddings against PostgreSQL + pgvector (Phase 5): schema rules, TenantDatabase access and the
embed_document_chunks service.

World: companies A and B, each with a policy and an UPLOADED document whose (synthetic) extraction is chunked by
Phase 4. A deterministic fake provider stands in for the model (tests/unit/test_embedding_model.py covers the real
one). Each test runs in a rolled-back transaction; a step expected to fail runs in ``db.transaction()``.
"""

import hashlib
import logging
import math
import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest
from psycopg import errors

from app.chunking import ChunkingConfig, DocumentChunks, chunk_document
from app.embeddings import EMBEDDING_DIMENSION, EmbeddingError, EmbeddingFailure, embed_document_chunks
from app.ingestion import DocumentExtractionResult, ExtractedPage
from app.models import UserRole
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase
from app.tokens import TokenIdentity

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
OTHER = {"A": "B", "B": "A"}
PAGES = ("SECTION A - COVER\n\nThe insurer will pay reasonable hospital expenses up to ₹5,00,000.",
         "",
         "SECTION B - EXCLUSIONS\n\n" + " ".join(f"Exclusion {n:02d} applies to the insured person." for n in range(12)))


def fake_vector(text: str, model_id: str) -> list[float]:
    digest = hashlib.sha256(f"{model_id}|{text}".encode()).digest()
    return [((digest[i % 32] + i) % 101) / 101.0 for i in range(EMBEDDING_DIMENSION)]


class FakeProvider:
    """Deterministic stand-in for the model: a vector derived from the text, recording every batch."""

    dimension = EMBEDDING_DIMENSION

    def __init__(self, model_id: str = "test/fake-embedder",
                 tamper: Callable[[list[list[float]]], Any] | None = None) -> None:
        self.model_id, self.tamper, self.calls = model_id, tamper, []

    def embed_batch(self, texts: list[str]) -> Any:
        self.calls.append(list(texts))
        vectors = [fake_vector(text, self.model_id) for text in texts]
        return self.tamper(vectors) if self.tamper else vectors


@pytest.fixture
def world(insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for company in ("A", "B"):
        rows[company] = insert_company(name=f"Company {company}", slug=f"embed-{company.lower()}")
        rows[f"{company}-policy"] = insert_policy(company_id=rows[company]["id"], policy_number="POL-001")
        rows[f"{company}-doc"] = insert_policy_document(policy_id=rows[f"{company}-policy"]["id"], version=2)
    return rows


def _chunks(document: Row, config: ChunkingConfig | None = None, **changes: Any) -> DocumentChunks:
    extraction = DocumentExtractionResult(**{
        "document_id": document["id"], "policy_id": document["policy_id"], "document_key": document["document_key"],
        "version": document["version"], "original_filename": document["original_filename"],
        "checksum_sha256": document["checksum_sha256"],
        "pages": tuple(ExtractedPage(page_number=n, text=text) for n, text in enumerate(PAGES, start=1)),
        **changes})
    return chunk_document(extraction, config)


def _scoped(db: Conn, company: Row) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company["id"]))


def _rows(db: Conn, document: Row) -> list[Row]:
    return db.execute("SELECT * FROM chunk_embeddings WHERE document_id = %s ORDER BY embedding_model, chunk_index",
                      (document["id"],)).fetchall()


def _count(db: Conn) -> int:
    return db.execute("SELECT count(*) AS n FROM chunk_embeddings").fetchone()["n"]


def _failure(db: Conn, call: Callable[[], Any]) -> EmbeddingError:
    with pytest.raises(EmbeddingError) as excinfo, db.transaction():
        call()
    return excinfo.value


# --- Persistence and provenance --------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_embeds_its_document_with_exact_chunk_provenance(
    db: Conn, world: dict[str, Row], company: str
) -> None:
    document, provider = world[f"{company}-doc"], FakeProvider()
    chunks = _chunks(document)

    result = embed_document_chunks(_scoped(db, world[company]), provider, chunks)

    stored = _scoped(db, world[company]).list_chunk_embeddings(document["id"], provider.model_id)
    assert (result.chunk_count, result.inserted, result.existing) == (len(chunks.chunks), len(chunks.chunks), 0)
    assert len(stored) == len(chunks.chunks) > 1
    for chunk, row in zip(chunks.chunks, stored, strict=True):
        assert (row.company_id, row.policy_id, row.document_id, row.document_version) == (
            world[company]["id"], document["policy_id"], document["id"], 2)
        assert (row.chunk_id, row.chunk_index, row.page_start, row.page_end, row.section_heading) == (
            chunk.chunk_id, chunk.chunk_index, chunk.page_start, chunk.page_end, chunk.section_heading)
        assert row.chunk_text == chunk.text  # the exact Phase 4 text
        assert (row.embedding_model, row.embedding_dimension, len(row.embedding)) == (provider.model_id, 384, 384)
        assert all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(row.embedding, fake_vector(chunk.text,
                                                                                               provider.model_id)))
    assert [(r.page_start, r.page_end) for r in stored][-1][1] == 3  # the empty page 2 never shifts page numbers


def test_the_embedding_input_is_exactly_each_chunk_text_in_order(db: Conn, world: dict[str, Row]) -> None:
    provider, chunks = FakeProvider(), _chunks(world["A-doc"])

    embed_document_chunks(_scoped(db, world["A"]), provider, chunks)

    assert provider.calls == [[chunk.text for chunk in chunks.chunks]]  # no heading, ids or metadata added


def test_re_embedding_the_same_chunks_is_idempotent(db: Conn, world: dict[str, Row]) -> None:
    tenant_db, chunks = _scoped(db, world["A"]), _chunks(world["A-doc"])
    embed_document_chunks(tenant_db, FakeProvider(), chunks)
    before = _rows(db, world["A-doc"])

    again = embed_document_chunks(tenant_db, FakeProvider(), chunks)

    assert (again.inserted, again.existing) == (0, len(chunks.chunks))
    assert _rows(db, world["A-doc"]) == before  # same ids, vectors and created_at: nothing duplicated or replaced


def test_a_different_chunking_is_refused_not_stored_beside_the_existing_one(db: Conn, world: dict[str, Row]) -> None:
    tenant_db = _scoped(db, world["A"])
    embed_document_chunks(tenant_db, FakeProvider(), _chunks(world["A-doc"]))
    before = _rows(db, world["A-doc"])

    failure = _failure(db, lambda: embed_document_chunks(tenant_db, FakeProvider(),
                                                         _chunks(world["A-doc"], ChunkingConfig(max_chars=200))))

    assert failure.reason is EmbeddingFailure.CONFLICTING_CHUNKS
    assert _rows(db, world["A-doc"]) == before


def test_embeddings_of_different_models_coexist_and_stay_distinguishable(db: Conn, world: dict[str, Row]) -> None:
    tenant_db, chunks = _scoped(db, world["A"]), _chunks(world["A-doc"])

    embed_document_chunks(tenant_db, FakeProvider("test/model-one"), chunks)
    embed_document_chunks(tenant_db, FakeProvider("test/model-two"), chunks)

    one = tenant_db.list_chunk_embeddings(world["A-doc"]["id"], "test/model-one")
    two = tenant_db.list_chunk_embeddings(world["A-doc"]["id"], "test/model-two")
    assert len(one) == len(two) == len(chunks.chunks)
    assert {row.embedding_model for row in one} == {"test/model-one"}
    assert [row.embedding for row in one] != [row.embedding for row in two]


# --- Batch, validation and failures ----------------------------------------------------------------------------


def _with(index: int, value: float) -> Callable[[list[list[float]]], list[list[float]]]:
    def tamper(vectors: list[list[float]]) -> list[list[float]]:
        vectors[1][index] = value
        return vectors
    return tamper


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [(_with(5, float("nan")), EmbeddingFailure.INVALID_VECTOR),
     (_with(5, float("inf")), EmbeddingFailure.INVALID_VECTOR),
     (_with(5, float("-inf")), EmbeddingFailure.INVALID_VECTOR),
     (lambda vectors: [vectors[0][:-1]] + vectors[1:], EmbeddingFailure.INVALID_VECTOR),
     (lambda vectors: vectors[:-1], EmbeddingFailure.VECTOR_COUNT_MISMATCH),
     (lambda vectors: vectors + [vectors[0]], EmbeddingFailure.VECTOR_COUNT_MISMATCH)],
    ids=["nan", "infinity", "minus-infinity", "wrong-dimension", "one-vector-short", "one-vector-extra"],
)
def test_malformed_provider_output_stores_nothing(
    db: Conn, world: dict[str, Row], tamper: Callable[[list[list[float]]], Any], reason: EmbeddingFailure
) -> None:
    failure = _failure(db, lambda: embed_document_chunks(_scoped(db, world["A"]), FakeProvider(tamper=tamper),
                                                         _chunks(world["A-doc"])))

    assert failure.reason is reason
    assert _count(db) == 0


def test_a_provider_failure_stores_nothing(db: Conn, world: dict[str, Row]) -> None:
    class FailingProvider(FakeProvider):
        def embed_batch(self, texts: list[str]) -> Any:
            raise EmbeddingError(EmbeddingFailure.PROVIDER_FAILURE)

    failure = _failure(db, lambda: embed_document_chunks(_scoped(db, world["A"]), FailingProvider(),
                                                         _chunks(world["A-doc"])))

    assert failure.reason is EmbeddingFailure.PROVIDER_FAILURE and _count(db) == 0


def test_a_database_failure_mid_batch_rolls_the_whole_batch_back(
    db: Conn, world: dict[str, Row], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_insert, calls = TenantDatabase.insert_chunk_embedding, []

    def failing_second_insert(self: TenantDatabase, **values: Any) -> Any:
        calls.append(values["chunk_index"])
        if len(calls) == 2:
            raise errors.DiskFull("simulated database failure")
        return real_insert(self, **values)

    monkeypatch.setattr(TenantDatabase, "insert_chunk_embedding", failing_second_insert)

    with pytest.raises(errors.DiskFull), db.transaction():
        embed_document_chunks(_scoped(db, world["A"]), FakeProvider(), _chunks(world["A-doc"]))

    assert calls == [0, 1]  # one row was written before the failure...
    assert _count(db) == 0  # ...and the transaction removed them


def test_an_unsupported_model_dimension_is_refused_before_anything_runs(db: Conn, world: dict[str, Row]) -> None:
    provider = FakeProvider()
    provider.dimension = 768

    failure = _failure(db, lambda: embed_document_chunks(_scoped(db, world["A"]), provider, _chunks(world["A-doc"])))

    assert failure.reason is EmbeddingFailure.UNSUPPORTED_MODEL and provider.calls == []


@pytest.mark.parametrize(("changes", "reason"),
                         [({"checksum_sha256": "cd" * 32}, EmbeddingFailure.PROVENANCE_MISMATCH),
                          ({"version": 7}, EmbeddingFailure.PROVENANCE_MISMATCH)],
                         ids=["other-file", "other-version"])
def test_chunks_that_do_not_match_the_document_record_are_refused(
    db: Conn, world: dict[str, Row], changes: dict[str, Any], reason: EmbeddingFailure
) -> None:
    provider = FakeProvider()

    failure = _failure(db, lambda: embed_document_chunks(_scoped(db, world["A"]), provider,
                                                         _chunks(world["A-doc"], **changes)))

    assert failure.reason is reason and provider.calls == [] and _count(db) == 0


def test_an_inactive_document_is_not_embedded(db: Conn, world: dict[str, Row], insert_policy_document: Insert) -> None:
    document = insert_policy_document(policy_id=world["A-policy"]["id"], version=3, status="INACTIVE")

    failure = _failure(db, lambda: embed_document_chunks(_scoped(db, world["A"]), FakeProvider(), _chunks(document)))

    assert failure.reason is EmbeddingFailure.INACTIVE and _count(db) == 0


# --- Database rules --------------------------------------------------------------------------------------------


def _row_values(document: Row, **changes: Any) -> dict[str, Any]:
    return {"policy_id": document["policy_id"], "document_id": document["id"], "document_version": document["version"],
            "chunk_id": uuid.uuid4(), "chunk_index": 0, "page_start": 1, "page_end": 1, "section_heading": None,
            "chunk_text": "The insurer will pay.", "embedding_model": "test/direct",
            "embedding": [0.5] * EMBEDDING_DIMENSION, **changes}


@pytest.mark.parametrize(
    ("changes", "error"),
    [({"embedding": [float("nan")] * 384}, errors.DataException), ({"embedding": [float("inf")] * 384},
                                                                    errors.DataException),
     ({"embedding": [0.5] * 383}, errors.DataException), ({"chunk_text": " padded "}, errors.CheckViolation),
     ({"chunk_text": ""}, errors.CheckViolation), ({"page_start": 3, "page_end": 2}, errors.CheckViolation),
     ({"chunk_index": -1}, errors.CheckViolation), ({"section_heading": ""}, errors.CheckViolation),
     ({"embedding_model": ""}, errors.CheckViolation)],
    ids=["nan", "infinity", "wrong-dimension", "padded-text", "empty-text", "pages-reversed", "negative-index",
         "empty-heading", "empty-model"],
)
def test_the_database_itself_rejects_invalid_embedding_rows(
    db: Conn, world: dict[str, Row], changes: dict[str, Any], error: type[Exception]
) -> None:
    with pytest.raises(error), db.transaction():
        _scoped(db, world["A"]).insert_chunk_embedding(**_row_values(world["A-doc"], **changes))

    assert _count(db) == 0


@pytest.mark.security
@pytest.mark.parametrize(
    "changes",
    [{"document_version": 9}, {"policy_id": "other-policy-of-A"}, {"document_id": "B-doc"},
     {"document_id": "B-doc", "policy_id": "B-policy", "document_version": 2}, {"document_id": "missing"}],
    ids=["wrong-version", "wrong-policy", "other-tenants-document", "other-tenants-full-provenance",
         "missing-document"],
)
def test_provenance_cannot_cross_tenants_or_disagree_with_the_document(
    db: Conn, world: dict[str, Row], insert_policy: Insert, changes: dict[str, Any]
) -> None:
    references = {"other-policy-of-A": insert_policy(company_id=world["A"]["id"])["id"],
                  "B-doc": world["B-doc"]["id"], "B-policy": world["B-policy"]["id"], "missing": uuid.uuid4()}
    resolved = {key: references.get(value, value) for key, value in changes.items()}

    with pytest.raises(errors.ForeignKeyViolation) as excinfo, db.transaction():
        _scoped(db, world["A"]).insert_chunk_embedding(**_row_values(world["A-doc"], **resolved))

    assert excinfo.value.diag.constraint_name == "chunk_embeddings_document_fkey"
    assert _count(db) == 0


def test_a_document_with_embeddings_cannot_be_deleted_and_nothing_cascades(db: Conn, world: dict[str, Row]) -> None:
    tenant_db = _scoped(db, world["A"])
    embed_document_chunks(tenant_db, FakeProvider(), _chunks(world["A-doc"]))

    with pytest.raises(errors.RestrictViolation) as excinfo, db.transaction():
        tenant_db.delete_document(world["A-doc"]["id"])

    assert excinfo.value.diag.constraint_name == "chunk_embeddings_document_fkey"
    assert _count(db) == len(_chunks(world["A-doc"]).chunks)


# --- Tenant security -------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_a_tenant_cannot_read_or_delete_another_tenants_embeddings(
    db: Conn, world: dict[str, Row], attacker: str
) -> None:
    victim = OTHER[attacker]
    embed_document_chunks(_scoped(db, world[victim]), FakeProvider(), _chunks(world[f"{victim}-doc"]))
    victim_rows = _rows(db, world[f"{victim}-doc"])
    attacker_db = _scoped(db, world[attacker])

    for row in victim_rows:
        assert attacker_db.get_chunk_embedding(row["id"]) is None
        assert attacker_db.delete_chunk_embedding(row["id"]) is False
    assert attacker_db.list_chunk_embeddings(world[f"{victim}-doc"]["id"], "test/fake-embedder") == []
    guessed = uuid.uuid4()
    assert (attacker_db.get_chunk_embedding(guessed), attacker_db.delete_chunk_embedding(guessed)) == (None, False)
    assert _rows(db, world[f"{victim}-doc"]) == victim_rows  # every row and column intact


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_a_tenant_cannot_embed_another_tenants_document(db: Conn, world: dict[str, Row], attacker: str) -> None:
    victim = OTHER[attacker]
    provider = FakeProvider()
    attacker_db = _scoped(db, world[attacker])

    foreign = _failure(db, lambda: embed_document_chunks(attacker_db, provider, _chunks(world[f"{victim}-doc"])))
    missing = _failure(db, lambda: embed_document_chunks(attacker_db, provider,
                                                         _chunks(world[f"{victim}-doc"], document_id=uuid.uuid4())))

    assert (foreign.reason, str(foreign)) == (missing.reason, str(missing)) == (
        EmbeddingFailure.NOT_FOUND, "Embedding failed: NOT_FOUND")
    assert provider.calls == [] and _count(db) == 0  # the victim's text never reached the provider


@pytest.mark.security
def test_a_tenant_can_delete_its_own_embedding(db: Conn, world: dict[str, Row]) -> None:
    tenant_db = _scoped(db, world["A"])
    embed_document_chunks(tenant_db, FakeProvider(), _chunks(world["A-doc"]))
    first = tenant_db.list_chunk_embeddings(world["A-doc"]["id"], "test/fake-embedder")[0]

    assert tenant_db.delete_chunk_embedding(first.id) is True
    assert tenant_db.get_chunk_embedding(first.id) is None
    assert tenant_db.delete_chunk_embedding(first.id) is False


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_embedding_follows_the_verified_identitys_tenant(db: Conn, world: dict[str, Row], company: str) -> None:
    identity = TokenIdentity(user_id=uuid.uuid4(), company_id=world[company]["id"], role=UserRole.COMPANY_ADMIN)
    tenant_db = TenantDatabase(db, resolve_tenant(identity))

    embed_document_chunks(tenant_db, FakeProvider(), _chunks(world[f"{company}-doc"]))
    foreign = _failure(db, lambda: embed_document_chunks(tenant_db, FakeProvider(),
                                                         _chunks(world[f"{OTHER[company]}-doc"])))

    assert {row["company_id"] for row in _rows(db, world[f"{company}-doc"])} == {world[company]["id"]}
    assert foreign.reason is EmbeddingFailure.NOT_FOUND


@pytest.mark.security
def test_embedding_logs_carry_no_text_vectors_or_tenant_ids(
    db: Conn, world: dict[str, Row], caplog: pytest.LogCaptureFixture
) -> None:
    chunks = _chunks(world["A-doc"])

    with caplog.at_level(logging.INFO, logger="app"):
        embed_document_chunks(_scoped(db, world["A"]), FakeProvider(), chunks)
        _failure(db, lambda: embed_document_chunks(_scoped(db, world["A"]), FakeProvider(), _chunks(world["B-doc"])))
    logs = caplog.text

    assert f"document embedded document_id={world['A-doc']['id']} model=test/fake-embedder" in logs
    assert "reason=NOT_FOUND" in logs
    for secret in ("hospital", "Exclusion", "₹", str(fake_vector(chunks.chunks[0].text, "test/fake-embedder")[0]),
                   str(world["A"]["id"]), str(world["B"]["id"]), world["A-doc"]["storage_reference"]):
        assert secret not in logs
