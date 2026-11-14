"""The search API against PostgreSQL (Phase 6): ``POST /search`` through the real authentication, RBAC, tenant and
transaction chain. The provider dependency is replaced by the fake query provider of test_retrieval.py, and the
connection by the test's rolled-back one.
"""

import uuid
from collections.abc import Iterator
from types import MappingProxyType
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

import app.api.search as search_module
import app.rbac as rbac
from app.api.database import database_connection
from app.api.search import SEARCH_UNAVAILABLE, search_provider
from app.auth import AuthenticatedUser
from app.config import Settings
from app.embeddings import EmbeddingError, EmbeddingFailure
from app.main import create_app
from app.models import UserRole, UserStatus
from app.rbac import FORBIDDEN_MESSAGE, ROLE_PERMISSIONS, Permission
from app.tokens import issue_access_token
from tests.conftest import RsaKeyPair
from tests.database.test_retrieval import MODEL, OTHER, QueryProvider, world  # noqa: F401  (world is a fixture)

pytestmark = pytest.mark.database

Row = dict[str, Any]
Conn = psycopg.Connection[Any]
HIT_FIELDS = {"chunk_id", "document_id", "policy_id", "document_version", "chunk_index", "page_start", "page_end",
              "section_heading", "chunk_text", "semantic_rank", "semantic_score", "keyword_rank", "keyword_score",
              "hybrid_score"}


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def reached() -> list[str]:
    return []


@pytest.fixture
def api(token_settings: Settings, db: Conn, reached: list[str]) -> Iterator[TestClient]:
    app = create_app(token_settings)
    provider = QueryProvider()

    def test_connection() -> Iterator[Conn]:
        reached.append("connection")
        yield db

    def test_provider() -> QueryProvider:
        reached.append("provider")
        return provider

    app.dependency_overrides[database_connection] = test_connection
    app.dependency_overrides[search_provider] = test_provider
    with TestClient(app) as client:
        yield client


def _headers(world: dict[str, Row], settings: Settings, company: str, role: UserRole) -> dict[str, str]:
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=world[company]["id"], email="user@acme.example",
                             full_name="User", role=role, status=UserStatus.ACTIVE)
    return {"Authorization": f"Bearer {issue_access_token(user, settings)}"}


def _connection(db: Conn) -> Any:
    """A connection dependency (a generator, as FastAPI expects) handing out the test's rolled-back connection."""

    def connection() -> Iterator[Conn]:
        yield db

    return connection


def _chunk_names(world: dict[str, Row], response: Any) -> list[str]:
    by_chunk = {str(row.chunk_id): name for name, row in world.items() if hasattr(row, "chunk_id")}
    return [by_chunk[hit["chunk_id"]] for hit in response.json()["hits"]]


@pytest.mark.security
@pytest.mark.parametrize("role", [UserRole.STAFF, UserRole.COMPANY_ADMIN])
@pytest.mark.parametrize("company", ["A", "B"])
def test_staff_and_admins_search_only_their_own_company(
    api: TestClient, world: dict[str, Row], token_settings: Settings, company: str, role: UserRole
) -> None:
    response = api.post("/search", json={"query": "hospital expenses", "top_k": 5},
                        headers=_headers(world, token_settings, company, role))

    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"query", "filters", "top_k", "method", "embedding_model", "hits"}
    assert (body["method"], body["embedding_model"], body["query"]) == ("hybrid", MODEL, "hospital expenses")
    names = _chunk_names(world, response)
    assert names and all(name.startswith(company.lower() + "-") for name in names)
    assert all(set(hit) == HIT_FIELDS for hit in body["hits"])  # no company, vector or storage reference
    assert str(world[company]["id"]) not in response.text and str(world[OTHER[company]]["id"]) not in response.text


@pytest.mark.security
@pytest.mark.parametrize("authorization", [None, "Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz"],
                         ids=["missing", "scheme-only", "malformed", "other-scheme"])
def test_unauthenticated_search_is_401_before_the_model_or_database(
    api: TestClient, reached: list[str], authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = api.post("/search", json={"query": "hospital expenses"}, headers=headers)

    assert response.status_code == 401 and reached == []


@pytest.mark.security
def test_a_role_without_search_use_is_403_before_the_model_or_database(
    api: TestClient, world: dict[str, Row], token_settings: Settings, reached: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reduced = {role: granted - {Permission.SEARCH_USE} for role, granted in ROLE_PERMISSIONS.items()}
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType(reduced))

    responses = [api.post("/search", json={"query": "hospital expenses"},
                          headers=_headers(world, token_settings, "A", role)) for role in UserRole]

    assert [(r.status_code, r.json()) for r in responses] == [(403, {"detail": FORBIDDEN_MESSAGE})] * 2
    assert reached == []


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_client_tenant_values_never_change_the_searched_company(
    api: TestClient, world: dict[str, Row], token_settings: Settings, attacker: str
) -> None:
    victim = str(world[OTHER[attacker]]["id"])
    headers = _headers(world, token_settings, attacker, UserRole.STAFF) | {
        "X-Company-Id": victim, "X-Tenant-Id": victim, "X-Role": "COMPANY_ADMIN",
        "Cookie": f"company_id={victim}; tenant_id={victim}"}

    claimed = api.post("/search", json={"query": "hospital expenses"}, headers=headers,
                       params={"company_id": victim, "tenant_id": victim})
    in_body = api.post("/search", json={"query": "hospital expenses", "company_id": victim}, headers=headers)
    in_filters = api.post("/search", json={"query": "hospital expenses", "filters": {"company_id": victim}},
                          headers=headers)

    assert claimed.status_code == 200
    assert all(name.startswith(attacker.lower() + "-") for name in _chunk_names(world, claimed))
    assert (in_body.status_code, in_filters.status_code) == (422, 422)


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_a_foreign_policy_or_document_filter_returns_no_hits(
    api: TestClient, world: dict[str, Row], token_settings: Settings, attacker: str
) -> None:
    victim = OTHER[attacker]
    headers = _headers(world, token_settings, attacker, UserRole.COMPANY_ADMIN)

    responses = [api.post("/search", json={"query": "hospital expenses", "filters": filters}, headers=headers)
                 for filters in ({"policy_id": str(world[f"{victim}-health"]["id"])},
                                 {"document_id": str(world[f"{victim}-H"]["id"])},
                                 {"policy_id": str(uuid.uuid4())})]

    assert [(r.status_code, r.json()["hits"]) for r in responses] == [(200, [])] * 3  # foreign = unknown


@pytest.mark.parametrize("body", [{"query": ""}, {"query": "   "}, {"query": 42}, {"query": "x" * 1001},
                                  {"query": "hospital", "top_k": 0}, {"query": "hospital", "top_k": 51},
                                  {"query": "hospital", "top_k": "5"},
                                  {"query": "hospital", "filters": {"insurance_type": "LIFE"}},
                                  {"query": "hospital", "filters": {"policy_id": "not-a-uuid"}},
                                  {"query": "hospital", "answer": True}],
                         ids=["empty", "whitespace", "not-text", "too-long", "top-k-zero", "top-k-too-big",
                              "top-k-text", "unknown-insurance-type", "bad-policy-id", "unknown-field"])
def test_invalid_search_requests_are_422(api: TestClient, world: dict[str, Row], token_settings: Settings,
                                         body: dict[str, Any]) -> None:
    response = api.post("/search", json=body, headers=_headers(world, token_settings, "A", UserRole.STAFF))

    assert response.status_code == 422


@pytest.mark.security
def test_an_unavailable_model_is_a_generic_503(
    token_settings: Settings, db: Conn, world: dict[str, Row], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Unavailable:
        def __init__(self) -> None:
            raise EmbeddingError(EmbeddingFailure.PROVIDER_UNAVAILABLE)

    class Failing(QueryProvider):
        def embed_batch(self, texts: list[str]) -> list[list[float]]:
            raise EmbeddingError(EmbeddingFailure.PROVIDER_FAILURE)

    headers = _headers(world, token_settings, "A", UserRole.STAFF)
    monkeypatch.setattr(search_module, "FastEmbedProvider", Unavailable)
    unloadable = create_app(token_settings)
    unloadable.dependency_overrides[database_connection] = _connection(db)
    failing = create_app(token_settings)
    failing.dependency_overrides[database_connection] = _connection(db)
    failing.dependency_overrides[search_provider] = Failing

    responses = []
    for app in (unloadable, failing):
        with TestClient(app) as client:
            responses.append(client.post("/search", json={"query": "hospital expenses"}, headers=headers))

    assert [(r.status_code, r.json()) for r in responses] == [(503, {"detail": SEARCH_UNAVAILABLE})] * 2


def test_the_provider_is_loaded_once_per_application(
    token_settings: Settings, db: Conn, world: dict[str, Row], monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[QueryProvider] = []

    def build() -> QueryProvider:
        built.append(QueryProvider())
        return built[-1]

    monkeypatch.setattr(search_module, "FastEmbedProvider", build)
    app = create_app(token_settings)
    app.dependency_overrides[database_connection] = _connection(db)
    headers = _headers(world, token_settings, "A", UserRole.STAFF)

    with TestClient(app) as client:
        statuses = [client.post("/search", json={"query": "hospital expenses"}, headers=headers).status_code
                    for _ in range(3)]

    assert statuses == [200, 200, 200] and len(built) == 1
