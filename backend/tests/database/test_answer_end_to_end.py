"""The whole Phase 8-9 path with real models (markers ``database``, ``embedding_model`` and ``llm``):

    POST /chat → authentication → chat:use → tenant → Phase 6 retrieval (real embedding model, PostgreSQL)
        → evidence formatting → local LLM (qwen2.5:3b on Ollama) → verified verdict (Phase 9)
        → status + grounded answer + citations from stored provenance + the evidence it was given

The corpus is the fixed retrieval regression set of test_retrieval_quality.py. Only the database connection is the
test's (rolled back); the embedding and LLM providers are the application's own.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.database import database_connection
from app.auth import AuthenticatedUser
from app.config import Settings
from app.main import create_app
from app.models import UserRole, UserStatus
from app.tenant_db import TenantDatabase
from app.tokens import issue_access_token
from tests.conftest import RsaKeyPair
from tests.database.test_retrieval_quality import provider, tenant_db  # noqa: F401  (fixtures)

pytestmark = [pytest.mark.database, pytest.mark.embedding_model, pytest.mark.llm]

Conn = psycopg.Connection[Any]


def test_a_question_is_answered_end_to_end_from_the_tenants_own_evidence(
    db: Conn, tenant_db: TenantDatabase, jwt_keys: RsaKeyPair  # noqa: F811
) -> None:
    settings = Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem,
                        llm_max_output_tokens=160)
    app = create_app(settings)

    def connection() -> Iterator[Conn]:
        yield db

    app.dependency_overrides[database_connection] = connection
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=tenant_db.tenant.company_id, email="staff@acme.example",
                             full_name="Staff", role=UserRole.STAFF, status=UserStatus.ACTIVE)

    with TestClient(app) as client:
        response = client.post("/chat", json={"question": "What is the waiting period for maternity expenses?",
                                              "top_k": 3},
                               headers={"Authorization": f"Bearer {issue_access_token(user, settings)}"})

    body = response.json()
    assert response.status_code == 200, body
    assert (body["model"], body["embedding_model"], body["generated"]) == (
        "qwen2.5:3b", "Qdrant/bge-small-en-v1.5-onnx-Q", True)
    assert "9" in body["answer"], body["answer"]  # "covered after a waiting period of 9 months"
    assert body["evidence"][0]["section_heading"] == "SECTION 3 - MATERNITY"
    assert body["evidence_ids"] == [hit["chunk_id"] for hit in body["evidence"]]
    stored = {str(row.chunk_id) for row in tenant_db.list_chunk_embeddings(
        uuid.UUID(body["evidence"][0]["document_id"]), body["embedding_model"])}
    assert set(body["evidence_ids"]) <= stored  # every passage is one of the tenant's own stored chunks
    assert body["retrieval_ms"] >= 0 and body["generation_ms"] > 0
    # Phase 9: answered, citing the maternity clause with the stored provenance of a passage the model was shown.
    assert body["status"] == "ANSWERED"
    shown = {hit["chunk_id"]: hit for hit in body["evidence"]}
    assert body["citations"] and all(citation["chunk_id"] in shown for citation in body["citations"])
    for citation in body["citations"]:
        assert citation == {field: shown[citation["chunk_id"]][field] for field in citation}
    assert "SECTION 3 - MATERNITY" in {citation["section_heading"] for citation in body["citations"]}
