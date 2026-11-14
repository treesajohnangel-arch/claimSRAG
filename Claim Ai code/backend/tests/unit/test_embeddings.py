"""Unit tests for the embedding infrastructure (Phase 5), without a database or the model files.

The provider's own logic runs against a stand-in for the ONNX model; tests/unit/test_embedding_model.py runs the
real model (marker ``embedding_model``) and tests/database/test_chunk_embeddings.py the persistence.
"""

import ast
import inspect
import socket
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import app.embeddings as embeddings_module
from app.chunking import chunk_document
from app.config import BACKEND_DIR
from app.embeddings import (EMBEDDING_DIMENSION, FASTEMBED_MODEL_NAME, EmbeddingError, EmbeddingFailure,
                            FastEmbedProvider,
                            embed_document_chunks, embedding_inputs, validate_vectors)
from app.ingestion import DocumentExtractionResult, ExtractedPage

GOOD = [0.25] * EMBEDDING_DIMENSION


# --- Vector validation -----------------------------------------------------------------------------------------


def test_valid_vectors_are_returned_as_float_lists() -> None:
    assert validate_vectors([GOOD, [1] * EMBEDDING_DIMENSION], 2, EMBEDDING_DIMENSION) == [GOOD, [1.0] * 384]


@pytest.mark.parametrize(
    ("vectors", "count", "reason"),
    [([GOOD], 2, EmbeddingFailure.VECTOR_COUNT_MISMATCH), ([GOOD, GOOD], 1, EmbeddingFailure.VECTOR_COUNT_MISMATCH),
     (None, 1, EmbeddingFailure.VECTOR_COUNT_MISMATCH), ((GOOD,), 1, EmbeddingFailure.VECTOR_COUNT_MISMATCH),
     ([GOOD[:-1]], 1, EmbeddingFailure.INVALID_VECTOR), ([GOOD + [0.1]], 1, EmbeddingFailure.INVALID_VECTOR),
     ([[float("nan")] + GOOD[1:]], 1, EmbeddingFailure.INVALID_VECTOR),
     ([GOOD[:-1] + [float("inf")]], 1, EmbeddingFailure.INVALID_VECTOR),
     ([GOOD[:-1] + [float("-inf")]], 1, EmbeddingFailure.INVALID_VECTOR),
     ([GOOD[:-1] + [True]], 1, EmbeddingFailure.INVALID_VECTOR), ([GOOD[:-1] + ["0.1"]], 1,
                                                                   EmbeddingFailure.INVALID_VECTOR),
     ([GOOD[:-1] + [None]], 1, EmbeddingFailure.INVALID_VECTOR), (["not a vector"], 1,
                                                                  EmbeddingFailure.INVALID_VECTOR)],
    ids=["too-few", "too-many", "none", "not-a-list", "short", "long", "nan", "infinity", "minus-infinity", "bool",
         "string-value", "none-value", "string-vector"],
)
def test_malformed_vectors_are_rejected(vectors: Any, count: int, reason: EmbeddingFailure) -> None:
    with pytest.raises(EmbeddingError) as excinfo:
        validate_vectors(vectors, count, EMBEDDING_DIMENSION)

    assert excinfo.value.reason is reason


# --- Provider logic (stand-in model) ---------------------------------------------------------------------------


class _StandInModel:
    """Stands in for fastembed's TextEmbedding: yields numpy float32 vectors like the real one."""

    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result, self.error, self.calls = result, error, []

    def embed(self, texts: list[str]) -> Any:
        self.calls.append(texts)
        if self.error:
            raise self.error
        return self.result if self.result is not None else (np.full(384, 0.5, dtype=np.float32) for _ in texts)


def _provider(model: _StandInModel) -> FastEmbedProvider:
    provider = FastEmbedProvider.__new__(FastEmbedProvider)  # skips loading the real model files
    provider._model = model
    return provider


def test_the_provider_returns_one_float_vector_per_text_in_order() -> None:
    model = _StandInModel()

    vectors = _provider(model).embed_batch(["first", "second", "third"])

    assert model.calls == [["first", "second", "third"]]
    assert len(vectors) == 3 and all(len(v) == 384 and type(v[0]) is float for v in vectors)
    assert _provider(_StandInModel()).embed("single") == vectors[0]


@pytest.mark.parametrize(("texts", "reason"), [([], EmbeddingFailure.EMPTY_INPUT), ("text", EmbeddingFailure.EMPTY_INPUT),
                                               (None, EmbeddingFailure.EMPTY_INPUT),
                                               (["ok", ""], EmbeddingFailure.INVALID_INPUT),
                                               (["ok", "   "], EmbeddingFailure.INVALID_INPUT),
                                               (["ok", 7], EmbeddingFailure.INVALID_INPUT)],
                         ids=["empty-list", "bare-string", "none", "empty-text", "blank-text", "not-text"])
def test_the_provider_refuses_empty_or_invalid_input_before_the_model(texts: Any, reason: EmbeddingFailure) -> None:
    model = _StandInModel()

    with pytest.raises(EmbeddingError) as excinfo:
        _provider(model).embed_batch(texts)

    assert excinfo.value.reason is reason and model.calls == []


@pytest.mark.security
def test_a_model_failure_is_reported_without_its_message_or_the_text() -> None:
    model = _StandInModel(error=RuntimeError("onnx failure while reading: Sum insured ₹5,00,000"))

    with pytest.raises(EmbeddingError) as excinfo:
        _provider(model).embed_batch(["Sum insured ₹5,00,000"])

    assert excinfo.value.reason is EmbeddingFailure.PROVIDER_FAILURE
    assert "₹" not in str(excinfo.value) and "onnx" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None and excinfo.value.__suppress_context__


@pytest.mark.parametrize(
    "result",
    [[np.full(383, 0.5, dtype=np.float32)], [np.array([np.nan] * 384, dtype=np.float32)],
     [np.full(384, 0.5, dtype=np.float32)] * 2],
    ids=["wrong-dimension", "nan", "extra-vector"],
)
def test_malformed_model_output_is_rejected(result: Any) -> None:
    with pytest.raises(EmbeddingError) as excinfo:
        _provider(_StandInModel(result=result)).embed_batch(["one text"])

    assert excinfo.value.reason in {EmbeddingFailure.INVALID_VECTOR, EmbeddingFailure.VECTOR_COUNT_MISMATCH}


@pytest.mark.security
def test_missing_model_files_are_provider_unavailable_without_any_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Loading never tries to download unless asked to: not a single connection is attempted.
    attempts: list[Any] = []

    def record(sock: socket.socket, address: Any) -> None:
        attempts.append(address)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", record)

    with pytest.raises(EmbeddingError) as excinfo:
        FastEmbedProvider(tmp_path / "empty-cache")

    assert excinfo.value.reason is EmbeddingFailure.PROVIDER_UNAVAILABLE
    assert attempts == []


@pytest.mark.security
def test_the_provider_reads_only_local_files_unless_a_download_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    import fastembed

    seen: list[dict[str, Any]] = []

    class RecordingTextEmbedding:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

    monkeypatch.setattr(fastembed, "TextEmbedding", RecordingTextEmbedding)

    FastEmbedProvider()
    FastEmbedProvider(allow_download=True)

    assert [kwargs["local_files_only"] for kwargs in seen] == [True, False]
    assert {kwargs["model_name"] for kwargs in seen} == {FASTEMBED_MODEL_NAME}
    assert seen[0]["cache_dir"] == str(BACKEND_DIR / ".model_cache")


# --- Input and boundary ----------------------------------------------------------------------------------------


def test_the_embedding_input_is_exactly_the_chunk_text_without_headings_or_metadata() -> None:
    extraction = DocumentExtractionResult(
        document_id=uuid.uuid4(), policy_id=uuid.uuid4(), document_key="policy-wording", version=1,
        original_filename="p.pdf", checksum_sha256="ab" * 32,
        pages=(ExtractedPage(page_number=1, text="SECTION B - EXCLUSIONS\n\nCosmetic treatment is excluded."),
               ExtractedPage(page_number=2, text="Clause 2 applies.")))
    chunks = chunk_document(extraction)

    assert embedding_inputs(chunks) == [chunk.text for chunk in chunks.chunks]
    assert embedding_inputs(chunks) == ["SECTION B - EXCLUSIONS\n\nCosmetic treatment is excluded.\n\nClause 2 applies."]


@pytest.mark.security
def test_embedding_takes_no_tenant_or_storage_input() -> None:
    assert list(inspect.signature(embed_document_chunks).parameters) == ["db", "provider", "chunks"]
    with pytest.raises(TypeError):
        embed_document_chunks(object(), object(), object())  # type: ignore[arg-type]


@pytest.mark.security
def test_the_embedding_module_holds_no_sql_and_nothing_updates_stored_embeddings() -> None:
    # All data access goes through TenantDatabase (D-020); stored embeddings are immutable (no UPDATE anywhere).
    module_tree = ast.parse(Path(embeddings_module.__file__).read_text(encoding="utf-8"))
    sql = [node.value for node in ast.walk(module_tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
           and any(word in node.value.upper() for word in ("SELECT ", "INSERT ", "DELETE ", "UPDATE "))]
    updates = [path for path in sorted((BACKEND_DIR / "app").rglob("*.py"))
               if "UPDATE CHUNK_EMBEDDINGS" in " ".join(path.read_text(encoding="utf-8").upper().split())]

    assert sql == [] and updates == []
