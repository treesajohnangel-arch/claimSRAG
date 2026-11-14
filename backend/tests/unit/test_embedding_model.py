"""The real embedding model (Phase 5): BAAI/bge-small-en-v1.5 through fastembed, from backend/.model_cache.

Marker ``embedding_model``: deselected by default, run with ``pytest -m embedding_model``. The model files must be
in the local cache (downloaded once with ``FastEmbedProvider(allow_download=True)``). If they are missing, these
tests fail; they never download at test time.
"""

import math
import socket
from typing import Any

import pytest

from app.embeddings import (EMBEDDING_DIMENSION, EMBEDDING_MODEL_ID, FASTEMBED_MODEL_NAME, FastEmbedProvider)

pytestmark = pytest.mark.embedding_model

TEXTS = ["The insurer will pay reasonable hospital expenses for in-patient treatment.",
         "Pre-existing diseases are excluded for the first 36 months of cover.",
         "Sum insured: ₹5,00,000; co-payment 20% applies to every claim."]


@pytest.fixture(scope="module")
def provider() -> FastEmbedProvider:
    return FastEmbedProvider()


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_the_recorded_model_id_names_the_weights_fastembed_loads() -> None:
    # Guards the id stored on every row: if a fastembed upgrade maps the name to other weights, this fails.
    from fastembed import TextEmbedding

    (entry,) = [m for m in TextEmbedding.list_supported_models() if m["model"] == FASTEMBED_MODEL_NAME]

    assert (entry["sources"]["hf"], entry["dim"]) == (EMBEDDING_MODEL_ID, EMBEDDING_DIMENSION)


def test_vectors_have_the_declared_dimension_finite_values_and_unit_length(provider: FastEmbedProvider) -> None:
    vectors = provider.embed_batch(TEXTS)

    assert len(vectors) == 3
    for vector in vectors:
        assert len(vector) == EMBEDDING_DIMENSION and all(math.isfinite(value) for value in vector)
        assert math.isclose(math.sqrt(sum(value * value for value in vector)), 1.0, abs_tol=1e-4)


def test_embedding_is_repeatable_and_independent_of_the_batch(provider: FastEmbedProvider) -> None:
    first, again = provider.embed_batch(TEXTS), provider.embed_batch(TEXTS)
    alone = provider.embed(TEXTS[1])

    assert first == again
    assert all(math.isclose(a, b, abs_tol=1e-5) for a, b in zip(first[1], alone))


def test_related_policy_wording_is_closer_than_unrelated_text(provider: FastEmbedProvider) -> None:
    hospital, paraphrase, unrelated = provider.embed_batch([
        TEXTS[0], "Hospitalisation costs for admitted patients are covered by the insurer.",
        "The quarterly marketing newsletter is published every March."])

    assert _cosine(hospital, paraphrase) > _cosine(hospital, unrelated)


@pytest.mark.security
def test_the_model_loads_and_embeds_without_any_network_access(monkeypatch: pytest.MonkeyPatch) -> None:
    # Policy text stays local: loading the model and embedding make no network connection at all.
    attempts: list[Any] = []

    def refuse(sock: socket.socket, address: Any) -> None:
        attempts.append(address)
        raise OSError("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)

    vectors = FastEmbedProvider().embed_batch(TEXTS)

    assert attempts == [] and len(vectors) == 3
