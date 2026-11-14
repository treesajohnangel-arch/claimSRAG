"""The PDF upload, ``POST /policies/{policy_id}/documents/upload`` (D-035), against PostgreSQL and the real embedding
model: an admin's upload is stored, recorded, ingested, chunked and embedded in the token's tenant, and is then
searchable and answerable there (with page provenance), and nowhere else. The storage directory is a temporary one;
the LLM is scripted (the real model is exercised by the end-to-end run).
"""

import json
import re
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.chat import answer_provider
from app.api.database import database_connection
from app.api.policies import document_storage
from app.api.search import search_provider
from app.auth import AuthenticatedUser
from app.config import Settings
from app.embeddings import EmbeddingError, EmbeddingFailure, FastEmbedProvider
from app.llm import LLMOutput
from app.main import create_app
from app.models import UserRole, UserStatus
from app.storage import LocalDocumentStorage
from app.tokens import issue_access_token
from scripts.workspace import sample_pdf
from tests.conftest import RsaKeyPair
from tests.database.test_retrieval_quality import provider  # noqa: F401  (fixture)

pytestmark = [pytest.mark.database, pytest.mark.embedding_model]

Row = dict[str, Any]
Conn = psycopg.Connection[Any]
BLOCK = re.compile(r"BEGIN EVIDENCE (E\d+) ([0-9a-f]{24})\n[^\n]*\n(.*?)\nEND EVIDENCE \1 \2", re.DOTALL)


class ScriptedLLM:
    """Answers from the passage containing ``needle``, or says the evidence is insufficient."""

    model_id = "test/fake-llm"

    def __init__(self, needle: str | None) -> None:
        self.needle, self.users = needle, []

    def generate(self, system: str, user: str, json_schema: dict[str, Any] | None = None) -> LLMOutput:
        self.users.append(user)
        labels = [label for label, _, text in BLOCK.findall(user) if self.needle and self.needle in text]
        if not labels:
            verdict = {"answer": "The provided policy evidence is insufficient to determine the answer.",
                       "evidence": [], "status": "INSUFFICIENT"}
        else:
            verdict = {"answer": "Maternity expenses are covered after a waiting period of 9 months.",
                       "evidence": labels[:1], "status": "ANSWERED"}
        return LLMOutput(text=json.dumps(verdict), output_tokens=10)


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def tenants(insert_company: Callable[..., Row], insert_policy: Callable[..., Row]) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for name in ("A", "B"):
        rows[name] = insert_company(name=f"Company {name}", slug=f"upload-{name.lower()}")
        rows[f"{name}-policy"] = insert_policy(company_id=rows[name]["id"], policy_number="DH-100",
                                               name=f"Family Health {name}", insurance_type="HEALTH")
    return rows


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    return tmp_path / "storage"


@pytest.fixture
def make_client(token_settings: Settings, db: Conn, storage_dir: Path,
                provider: FastEmbedProvider) -> Iterator[Callable[..., TestClient]]:  # noqa: F811
    clients: list[TestClient] = []

    def make(settings: Settings = token_settings, embedder: Any = provider, llm: Any = None) -> TestClient:
        app = create_app(settings)

        def connection() -> Iterator[Conn]:
            yield db

        app.dependency_overrides[database_connection] = connection
        app.dependency_overrides[search_provider] = lambda: embedder
        app.dependency_overrides[document_storage] = lambda: LocalDocumentStorage(storage_dir)
        if llm is not None:
            app.dependency_overrides[answer_provider] = lambda: llm
        client = TestClient(app)
        clients.append(client.__enter__())
        return client

    yield make
    for client in clients:
        client.__exit__(None, None, None)


def _headers(tenants: dict[str, Row], settings: Settings, company: str,
             role: UserRole = UserRole.COMPANY_ADMIN) -> dict[str, str]:
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=tenants[company]["id"], email="user@upload.example",
                             full_name="User", role=role, status=UserStatus.ACTIVE)
    return {"Authorization": f"Bearer {issue_access_token(user, settings)}"}


def _upload(client: TestClient, tenants: dict[str, Row], settings: Settings, company: str, policy: str,
            data: bytes, role: UserRole = UserRole.COMPANY_ADMIN, **params: Any) -> Any:
    headers = _headers(tenants, settings, company, role) | {"Content-Type": "application/pdf"}
    query = {"filename": "family-health-wording.pdf"} | params
    return client.post(f"/policies/{tenants[policy]['id']}/documents/upload", content=data, params=query,
                       headers=headers)


def _stored_files(storage_dir: Path) -> list[Path]:
    return sorted(storage_dir.iterdir()) if storage_dir.exists() else []


@pytest.mark.security
def test_an_admins_upload_is_stored_indexed_and_searchable_with_page_provenance(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings, storage_dir: Path,
    db: Conn,
) -> None:
    client = make_client()
    data = sample_pdf()

    response = _upload(client, tenants, token_settings, "A", "A-policy", data)

    body = response.json()
    assert response.status_code == 201, body
    assert set(body) == {"document", "page_count", "chunk_count"}
    assert body["page_count"] == 4 and body["chunk_count"] >= 4
    document = body["document"]
    assert (document["original_filename"], document["document_key"], document["version"], document["status"],
            document["size_bytes"], document["content_type"]) == (
        "family-health-wording.pdf", "policy-wording", 1, "UPLOADED", len(data), "application/pdf")
    assert document["policy_id"] == str(tenants["A-policy"]["id"])
    assert "storage_reference" not in response.text and "company_id" not in response.text
    (stored,) = _stored_files(storage_dir)
    assert stored.read_bytes() == data and re.fullmatch(r"[A-Za-z0-9_-]{43}", stored.name)
    rows = db.execute("SELECT company_id, page_start, section_heading FROM chunk_embeddings WHERE document_id = %s",
                      (document["id"],)).fetchall()
    assert len(rows) == body["chunk_count"] and {row["company_id"] for row in rows} == {tenants["A"]["id"]}

    staff = _headers(tenants, token_settings, "A", UserRole.STAFF)
    search = client.post("/search", json={"query": "waiting period for maternity expenses", "top_k": 3},
                         headers=staff).json()
    first = search["hits"][0]
    assert (first["document_id"], first["page_start"], first["section_heading"]) == (
        document["id"], 3, "SECTION 3 - WAITING PERIODS")
    assert "Maternity expenses" in first["chunk_text"]


def test_chat_answers_from_the_uploaded_evidence_with_its_page_or_abstains(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings,
) -> None:
    uploaded = _upload(make_client(), tenants, token_settings, "A", "A-policy", sample_pdf()).json()["document"]
    staff = _headers(tenants, token_settings, "A", UserRole.STAFF)

    answered = make_client(llm=ScriptedLLM("Maternity expenses")).post(
        "/chat", json={"question": "What is the waiting period for maternity expenses?"}, headers=staff).json()
    abstained = make_client(llm=ScriptedLLM(None)).post(
        "/chat", json={"question": "Does the policy offer a no-claim bonus?"}, headers=staff).json()

    assert answered["status"] == "ANSWERED"
    (citation,) = answered["citations"]
    assert (citation["document_id"], citation["page_start"], citation["section_heading"],
            citation["document_version"]) == (uploaded["id"], 3, "SECTION 3 - WAITING PERIODS", 1)
    assert (abstained["status"], abstained["citations"]) == ("ABSTAINED", [])


@pytest.mark.security
def test_a_tenant_cannot_upload_into_or_find_another_tenants_policy(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings, storage_dir: Path,
) -> None:
    client = make_client()

    into_b = _upload(client, tenants, token_settings, "A", "B-policy", sample_pdf())
    with_b_in_query = _upload(client, tenants, token_settings, "A", "A-policy", sample_pdf(),
                              company_id=str(tenants["B"]["id"]), tenant_id=str(tenants["B"]["id"]))
    b_search = client.post("/search", json={"query": "waiting period for maternity expenses"},
                           headers=_headers(tenants, token_settings, "B", UserRole.STAFF)).json()
    b_documents = client.get(f"/policies/{tenants['B-policy']['id']}/documents",
                             headers=_headers(tenants, token_settings, "B")).json()

    assert (into_b.status_code, into_b.json()) == (404, {"detail": "Policy not found"})
    assert with_b_in_query.status_code == 201  # the client's company value is ignored: the upload is A's
    assert with_b_in_query.json()["document"]["policy_id"] == str(tenants["A-policy"]["id"])
    assert b_search["hits"] == [] and b_documents == []
    assert len(_stored_files(storage_dir)) == 1  # nothing was stored for the refused upload


@pytest.mark.security
@pytest.mark.parametrize(("role", "headers", "status"),
                         [(UserRole.STAFF, True, 403), (None, False, 401)], ids=["staff", "anonymous"])
def test_only_a_policy_manager_can_upload(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings, storage_dir: Path,
    role: UserRole | None, headers: bool, status: int,
) -> None:
    client = make_client()
    auth = _headers(tenants, token_settings, "A", role) if role else {}

    response = client.post(f"/policies/{tenants['A-policy']['id']}/documents/upload", content=sample_pdf(),
                           params={"filename": "x.pdf"}, headers=auth | {"Content-Type": "application/pdf"})

    assert response.status_code == status and _stored_files(storage_dir) == []


@pytest.mark.security
@pytest.mark.parametrize(
    ("data", "code"),
    [(b"<html><body>not a pdf</body></html>", "unreadable_pdf"),
     (b"%PDF-1.7\nthis is not really a pdf", "unreadable_pdf"),
     ("blank", "no_text")],
    ids=["html", "fake-header", "no-text-layer"],
)
def test_a_file_that_is_not_a_readable_pdf_is_refused_before_anything_is_stored(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings, storage_dir: Path,
    data: Any, code: str,
) -> None:
    if data == "blank":
        import pymupdf
        with pymupdf.open() as pdf:
            pdf.new_page()
            data = pdf.tobytes()

    response = _upload(make_client(), tenants, token_settings, "A", "A-policy", data)

    assert response.status_code == 422
    assert [(item["type"], item["loc"]) for item in response.json()["detail"]] == [(code, ["body", "file"])]
    assert _stored_files(storage_dir) == []


def test_an_empty_body_and_a_taken_version_are_refused_before_storing(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings, storage_dir: Path,
) -> None:
    client = make_client()
    first = _upload(client, tenants, token_settings, "A", "A-policy", sample_pdf())

    empty = _upload(client, tenants, token_settings, "A", "A-policy", b"")
    again = _upload(client, tenants, token_settings, "A", "A-policy", sample_pdf())
    next_version = _upload(client, tenants, token_settings, "A", "A-policy", sample_pdf(), version=2)

    assert first.status_code == 201 and empty.status_code == 422
    assert (again.status_code, again.json()) == (409, {"detail": "This document version already exists"})
    assert next_version.status_code == 201 and next_version.json()["document"]["version"] == 2
    assert len(_stored_files(storage_dir)) == 2


@pytest.mark.security
def test_a_body_over_the_limit_is_a_413_and_nothing_is_stored(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], jwt_keys: RsaKeyPair, storage_dir: Path,
) -> None:
    small = Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem,
                     max_upload_bytes=2048)

    response = _upload(make_client(settings=small), tenants, small, "A", "A-policy", sample_pdf())

    assert (response.status_code, response.json()) == (413, {"detail": "The request is too large"})
    assert _stored_files(storage_dir) == []


@pytest.mark.security
@pytest.mark.parametrize(("filename", "stored_as"),
                         [("../../etc/passwd.pdf", "passwd.pdf"), ("C:\\Users\\x\\policy.pdf", "policy.pdf"),
                          ("wording\x00\x1b.pdf", "wording.pdf"), ("..", "policy-document.pdf")],
                         ids=["traversal", "windows-path", "control-characters", "dot-dot"])
def test_the_client_file_name_is_display_metadata_only(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings, storage_dir: Path,
    filename: str, stored_as: str,
) -> None:
    response = _upload(make_client(), tenants, token_settings, "A", "A-policy", sample_pdf(), filename=filename)

    assert response.json()["document"]["original_filename"] == stored_as
    (stored,) = _stored_files(storage_dir)
    assert stored.parent == storage_dir and re.fullmatch(r"[A-Za-z0-9_-]{43}", stored.name)


def test_an_unavailable_embedding_model_is_a_generic_503(
    make_client: Callable[..., TestClient], tenants: dict[str, Row], token_settings: Settings,
) -> None:
    class Unavailable:
        model_id, dimension = "test/unavailable", 384

        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError(EmbeddingFailure.PROVIDER_FAILURE)

    response = _upload(make_client(embedder=Unavailable()), tenants, token_settings, "A", "A-policy", sample_pdf())

    assert (response.status_code, response.json()) == (503, {"detail": "Indexing is temporarily unavailable"})
