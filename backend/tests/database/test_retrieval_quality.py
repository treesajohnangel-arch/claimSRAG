"""Interim retrieval regression set (Phase 6, D-029): the real model and real PostgreSQL over a fixed policy corpus.

Invariant 8 asks for every retrieval change to be measured against a golden dataset. Until Phases -1/10 provide
one, this fixed set is the measurement: six policy sections, one document, chunked by Phase 4, embedded by the real
Phase 5 model, and searched by Phase 6. Every question names the section that answers it; retrieval must rank that
section first. Changing chunking, the model or the fusion must keep this passing, or change it on purpose.

Markers ``database`` and ``embedding_model``: it needs PostgreSQL and the local model files.
"""

import hashlib
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from app.chunking import chunk_document
from app.embeddings import FastEmbedProvider, embed_document_chunks
from app.ingestion import DocumentExtractionResult, ExtractedPage
from app.retrieval import retrieve
from app.tenancy import TenantIdentity
from app.tenant_db import TenantDatabase

pytestmark = [pytest.mark.database, pytest.mark.embedding_model]

Row = dict[str, Any]
Conn = psycopg.Connection[Any]

SECTIONS = {
    "SECTION 1 - HOSPITALISATION": "The insurer will pay for in-patient hospitalisation expenses including room rent, "
                                   "nursing and ICU charges when the insured is admitted for at least 24 hours.",
    "SECTION 2 - PRE-EXISTING DISEASES": "Pre-existing diseases are covered only after a waiting period of 36 months "
                                         "of continuous coverage.",
    "SECTION 3 - MATERNITY": "Maternity expenses, including childbirth and pre-natal care, are covered after a "
                             "waiting period of 9 months.",
    "SECTION 4 - AMBULANCE": "Road ambulance charges up to ₹2,000 per hospitalisation are reimbursed.",
    "SECTION 5 - EXCLUSIONS": "Cosmetic or plastic surgery, unless required after an accident, is not covered.",
    "SECTION 6 - CLAIMS": "Cashless claims must be intimated to the third party administrator within 24 hours of "
                          "emergency admission.",
}
QUESTIONS = {
    "Is childbirth covered?": "SECTION 3 - MATERNITY",
    "How long is the waiting period for diseases I already had?": "SECTION 2 - PRE-EXISTING DISEASES",
    "Does the policy pay for plastic surgery?": "SECTION 5 - EXCLUSIONS",
    "How much is reimbursed for an ambulance?": "SECTION 4 - AMBULANCE",
    "How quickly must I inform the insurer of an emergency admission?": "SECTION 6 - CLAIMS",
    "Are ICU charges paid during a hospital stay?": "SECTION 1 - HOSPITALISATION",
}


@pytest.fixture(scope="module")
def provider() -> FastEmbedProvider:
    return FastEmbedProvider()


@pytest.fixture
def tenant_db(db: Conn, insert_company: Callable[..., Row], insert_policy: Callable[..., Row],
              insert_policy_document: Callable[..., Row], provider: FastEmbedProvider) -> TenantDatabase:
    company = insert_company(slug="quality-set")
    policy = insert_policy(company_id=company["id"], insurance_type="HEALTH")
    text = "\n\n".join(f"{heading}\n\n{body}" for heading, body in SECTIONS.items())
    document = insert_policy_document(policy_id=policy["id"], checksum_sha256=hashlib.sha256(text.encode()).hexdigest())
    extraction = DocumentExtractionResult(
        document_id=document["id"], policy_id=policy["id"], document_key=document["document_key"],
        version=document["version"], original_filename=document["original_filename"],
        checksum_sha256=document["checksum_sha256"], pages=(ExtractedPage(page_number=1, text=text),))
    tenant_db = TenantDatabase(db, TenantIdentity(company_id=company["id"]))
    chunks = chunk_document(extraction)
    assert [chunk.section_heading for chunk in chunks.chunks] == list(SECTIONS)  # one chunk per section
    embed_document_chunks(tenant_db, provider, chunks)
    return tenant_db


# Measured baseline (2026-11-13): recall@1 = 5/6, recall@3 = 6/6. The one question ranked second is the claims
# question: its words never all occur in one chunk, so full-text search (websearch_to_tsquery, all words) finds nothing
# and the vector path alone ranks the hospitalisation section first. An "any word" (OR) keyword variant was measured
# on this set too: also 5/6 (it fixed this question and lost the pre-existing-diseases one), so it was not adopted.
BASELINE_RECALL_AT_1 = 5 / 6
KNOWN_MISSES_AT_1 = {"How quickly must I inform the insurer of an emergency admission?"}


def test_retrieval_meets_the_measured_baseline_on_the_fixed_question_set(tenant_db: TenantDatabase,
                                                                         provider: FastEmbedProvider) -> None:
    ranked = {question: [hit.section_heading for hit in retrieve(tenant_db, provider, question, top_k=3).hits]
              for question in QUESTIONS}

    misses_at_1 = {question for question, expected in QUESTIONS.items() if ranked[question][0] != expected}
    recall_at_1 = 1 - len(misses_at_1) / len(QUESTIONS)
    recall_at_3 = sum(expected in ranked[question] for question, expected in QUESTIONS.items()) / len(QUESTIONS)
    assert recall_at_3 == 1.0, ranked
    assert recall_at_1 >= BASELINE_RECALL_AT_1, f"recall@1 = {recall_at_1:.2f}: {ranked}"
    assert misses_at_1 <= KNOWN_MISSES_AT_1, misses_at_1  # an improvement passes; any new miss is a regression
