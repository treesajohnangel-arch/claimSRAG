"""Policy and document API without a database (Phase 2.4): authentication, RBAC, request schemas and response
contracts. tests/database/test_policy_api.py runs the same routes against PostgreSQL.

The connection dependency is replaced by a recorder, so these tests observe that a denied or invalid request never
opens a database connection and never calls a ``TenantDatabase`` method.
"""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.database import database_connection, tenant_database
from app.api.policies import DocumentResponse, PolicyResponse
from app.auth import AuthenticatedUser
from app.config import Settings
from app.errors import INTERNAL_ERROR_DETAIL
from app.main import create_app
from app.models import Policy, UserRole, UserStatus
from app.rbac import FORBIDDEN_MESSAGE
from app.tokens import issue_access_token
from tests.conftest import RsaKeyPair

ID = "00000000-0000-4000-8000-000000000001"
POLICY = {"policy_number": "POL-1", "name": "Health Cover", "insurance_type": "HEALTH"}
DOCUMENT = {"document_key": "policy-wording", "version": 1, "original_filename": "wording.pdf",
            "content_type": "application/pdf", "size_bytes": 1024, "checksum_sha256": "a" * 64}
READS = [("GET", "/policies", None), ("GET", f"/policies/{ID}", None), ("GET", f"/policies/{ID}/documents", None),
         ("GET", f"/documents/{ID}", None)]
MANAGES = [("POST", "/policies", POLICY), ("PATCH", f"/policies/{ID}", {"name": "New"}),
           ("DELETE", f"/policies/{ID}", None), ("POST", f"/policies/{ID}/documents", DOCUMENT),
           ("PATCH", f"/documents/{ID}", {"original_filename": "new.pdf"}),
           ("PUT", f"/documents/{ID}/status", {"status": "INACTIVE"}), ("DELETE", f"/documents/{ID}", None)]
ROUTE_IDS = [f"{method} {path.replace(ID, '{id}')}" for method, path, _ in READS + MANAGES]


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


class _UntouchableDatabase:
    """Stands in for TenantDatabase: any method call is recorded."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append(name)
            raise AssertionError(f"TenantDatabase.{name} reached")

        return record


@pytest.fixture
def opened() -> list[str]:
    """Each database connection a request opened, and each TenantDatabase method it called."""
    return []


@pytest.fixture
def api(token_settings: Settings, opened: list[str]) -> Iterator[TestClient]:
    app = create_app(token_settings)

    def recording_connection() -> Iterator[None]:
        opened.append("connection")
        yield None

    def untouchable_database() -> Iterator[_UntouchableDatabase]:
        yield _UntouchableDatabase(opened)

    app.dependency_overrides[database_connection] = recording_connection
    app.dependency_overrides[tenant_database] = untouchable_database
    with TestClient(app) as client:
        yield client


def _headers(settings: Settings, role: UserRole) -> dict[str, str]:
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=uuid.uuid4(), email="user@acme.example",
                             full_name="Test User", role=role, status=UserStatus.ACTIVE)
    return {"Authorization": f"Bearer {issue_access_token(user, settings)}"}


def _send(client: TestClient, method: str, path: str, body: Any, headers: dict[str, str] | None = None) -> Any:
    return client.request(method, path, json=body, headers=headers or {})


# --- Authentication and RBAC before any database work ---------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(("method", "path", "body"), READS + MANAGES, ids=ROUTE_IDS)
@pytest.mark.parametrize("authorization", [None, "Bearer", "Bearer not-a-jwt", "Basic dXNlcjpwYXNz"],
                         ids=["missing", "scheme-only", "malformed", "other-scheme"])
def test_every_route_requires_authentication_before_the_database(
    api: TestClient, opened: list[str], method: str, path: str, body: Any, authorization: str | None
) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = _send(api, method, path, body, headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert opened == []


@pytest.mark.security
@pytest.mark.parametrize(("method", "path", "body"), MANAGES, ids=ROUTE_IDS[len(READS):])
def test_staff_cannot_manage_and_never_reaches_the_database(
    api: TestClient, token_settings: Settings, opened: list[str], method: str, path: str, body: Any
) -> None:
    # Client-chosen role and permission values change nothing: they are not inputs.
    headers = _headers(token_settings, UserRole.STAFF) | {"X-Role": "COMPANY_ADMIN", "X-Permission": "policy:manage"}

    response = api.request(method, path, json=body, headers=headers,
                           params={"role": "COMPANY_ADMIN", "permission": "policy:manage"})

    assert response.status_code == 403
    assert response.json() == {"detail": FORBIDDEN_MESSAGE}
    assert opened == []


@pytest.mark.security
def test_a_staff_body_carrying_an_admin_role_is_still_denied_first(
    api: TestClient, token_settings: Settings, opened: list[str]
) -> None:
    # The permission check runs before the body is even validated: a STAFF request is 403, not 422.
    body = POLICY | {"role": "COMPANY_ADMIN", "permissions": ["policy:manage"], "company_id": str(uuid.uuid4())}

    response = api.post("/policies", json=body, headers=_headers(token_settings, UserRole.STAFF))

    assert response.status_code == 403
    assert opened == []


# --- Request schemas -----------------------------------------------------------------------------------------


INVALID_BODIES = [
    ("POST", "/policies", POLICY | {"company_id": str(uuid.uuid4())}, "extra_forbidden"),
    ("POST", "/policies", POLICY | {"role": "COMPANY_ADMIN"}, "extra_forbidden"),
    ("POST", "/policies", POLICY | {"permissions": ["policy:manage"]}, "extra_forbidden"),
    ("POST", "/policies", POLICY | {"id": str(uuid.uuid4())}, "extra_forbidden"),
    ("POST", "/policies", {k: v for k, v in POLICY.items() if k != "name"}, "missing"),
    ("POST", "/policies", POLICY | {"insurance_type": "LIFE"}, "enum"),
    ("POST", "/policies", POLICY | {"insurance_type": "health"}, "enum"),
    ("POST", "/policies", POLICY | {"status": "DRAFT"}, "enum"),
    ("POST", "/policies", POLICY | {"policy_number": 101}, "string_type"),
    ("PATCH", f"/policies/{ID}", {}, "value_error"),
    ("PATCH", f"/policies/{ID}", {"name": None}, "value_error"),
    ("PATCH", f"/policies/{ID}", {"status": "INACTIVE"}, "extra_forbidden"),
    ("PATCH", f"/policies/{ID}", {"company_id": str(uuid.uuid4())}, "extra_forbidden"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"storage_reference": "chosen/by-client.pdf"},
     "extra_forbidden"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"company_id": str(uuid.uuid4())}, "extra_forbidden"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"policy_id": str(uuid.uuid4())}, "extra_forbidden"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"version": 0}, "greater_than_equal"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"version": 2**31}, "less_than_equal"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"version": "1"}, "int_type"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"version": True}, "int_type"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"size_bytes": 0}, "greater_than"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"content_type": "text/html"}, "literal_error"),
    ("POST", f"/policies/{ID}/documents", DOCUMENT | {"status": "ACTIVE"}, "enum"),
    ("POST", f"/policies/{ID}/documents", {k: v for k, v in DOCUMENT.items() if k != "checksum_sha256"},
     "missing"),
    ("PATCH", f"/documents/{ID}", {}, "missing"),
    ("PATCH", f"/documents/{ID}", {"original_filename": "x.pdf", "storage_reference": "elsewhere"},
     "extra_forbidden"),
    ("PATCH", f"/documents/{ID}", {"original_filename": "x.pdf", "version": 2}, "extra_forbidden"),
    ("PATCH", f"/documents/{ID}", {"original_filename": "x.pdf", "policy_id": str(uuid.uuid4())},
     "extra_forbidden"),
    ("PUT", f"/documents/{ID}/status", {"status": "ACTIVE"}, "enum"),
    ("PUT", f"/documents/{ID}/status", {"status": "SUPERSEDED"}, "enum"),
    ("PUT", f"/documents/{ID}/status", {"status": "PROCESSED"}, "enum"),
    ("PUT", f"/documents/{ID}/status", {"status": "inactive"}, "enum"),
]


@pytest.mark.security
@pytest.mark.parametrize(("method", "path", "body", "error"), INVALID_BODIES,
                         ids=[f"{m} {p.replace(ID, '{id}')} {e}-{i}" for i, (m, p, _, e) in enumerate(INVALID_BODIES)])
def test_invalid_or_untrusted_request_fields_are_422_before_any_database_call(
    api: TestClient, token_settings: Settings, opened: list[str], method: str, path: str, body: Any, error: str
) -> None:
    response = _send(api, method, path, body, _headers(token_settings, UserRole.COMPANY_ADMIN))

    assert response.status_code == 422
    assert error in {item["type"] for item in response.json()["detail"]}
    assert [call for call in opened if call != "connection"] == []  # no TenantDatabase method was called


@pytest.mark.parametrize("path", ["/policies/not-a-uuid", "/documents/1", "/policies/1/documents"])
def test_resource_ids_must_be_uuids(api: TestClient, token_settings: Settings, opened: list[str], path: str) -> None:
    response = api.get(path, headers=_headers(token_settings, UserRole.STAFF))

    assert response.status_code == 422
    assert [call for call in opened if call != "connection"] == []


# --- Response contracts and unexpected errors -----------------------------------------------------------------


@pytest.mark.security
def test_responses_never_carry_the_company_or_storage_internals() -> None:
    assert set(PolicyResponse.model_fields) == {"id", "policy_number", "name", "insurance_type", "status",
                                                "created_at", "updated_at"}
    assert set(DocumentResponse.model_fields) == {"id", "policy_id", "document_key", "version", "original_filename",
                                                  "content_type", "size_bytes", "checksum_sha256", "status",
                                                  "created_at", "updated_at"}
    now = datetime.now(UTC)
    policy = Policy(id=uuid.uuid4(), company_id=uuid.uuid4(), policy_number="P", name="N", insurance_type="HOME",
                    status="ACTIVE", created_at=now, updated_at=now)
    assert "company_id" not in PolicyResponse.model_validate(policy, from_attributes=True).model_dump()


@pytest.mark.security
def test_an_unmapped_database_error_is_a_generic_500(token_settings: Settings) -> None:
    app: FastAPI = create_app(token_settings)

    class _FailingDatabase:
        def create_policy(self, **_: Any) -> None:
            raise psycopg.errors.UniqueViolation('duplicate key value violates unique constraint "secret_key" '
                                                 "DETAIL: Key (company_id)=(...) already exists.")

    def failing_database() -> Iterator[_FailingDatabase]:
        yield _FailingDatabase()

    app.dependency_overrides[tenant_database] = failing_database
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/policies", json=POLICY, headers=_headers(token_settings, UserRole.COMPANY_ADMIN))

    assert response.status_code == 500
    assert response.json() == {"detail": INTERNAL_ERROR_DETAIL}
    for leaked in ("secret_key", "duplicate", "company_id", "DETAIL"):
        assert leaked not in response.text


class _TransactionRecordingConnection:
    """Stands in for a psycopg connection: every query returns ``rows``, and the end of the transaction is recorded."""

    def __init__(self, events: list[str], rows: list[dict[str, Any]]) -> None:
        self.events, self.rows = events, rows

    @contextmanager
    def transaction(self) -> Iterator[None]:
        try:
            yield
        except BaseException:
            self.events.append("rolled back")
            raise
        self.events.append("committed")

    def cursor(self, row_factory: Any = None) -> "_TransactionRecordingConnection":
        return self

    def __enter__(self) -> "_TransactionRecordingConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any]) -> "_TransactionRecordingConnection":
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [("POST", "/policies", POLICY, (201, "committed")), ("DELETE", f"/policies/{ID}", None, (404, "rolled back"))],
    ids=["successful-write", "refused-write"],
)
def test_the_transaction_ends_before_the_response_is_sent(
    token_settings: Settings, method: str, path: str, body: Any, expected: tuple[int, str]
) -> None:
    # A client must never receive a success for a write whose commit has not happened (and could still fail).
    company_id, events = uuid.uuid4(), []
    now = datetime.now(UTC)
    row = {"id": uuid.uuid4(), "company_id": company_id, "policy_number": "POL-1", "name": "Health Cover",
           "insurance_type": "HEALTH", "status": "ACTIVE", "created_at": now, "updated_at": now}
    app = create_app(token_settings)

    def recording_connection() -> Iterator[_TransactionRecordingConnection]:
        yield _TransactionRecordingConnection(events, [row] if method == "POST" else [])

    class RecordResponseStart:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            async def recording_send(message: Any) -> None:
                if message["type"] == "http.response.start":
                    events.append("response started")
                await send(message)

            await self.inner(scope, receive, recording_send)

    app.dependency_overrides[database_connection] = recording_connection
    app.add_middleware(RecordResponseStart)
    user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=company_id, email="admin@acme.example",
                             full_name="Admin", role=UserRole.COMPANY_ADMIN, status=UserStatus.ACTIVE)
    with TestClient(app) as client:
        response = client.request(method, path, json=body,
                                  headers={"Authorization": f"Bearer {issue_access_token(user, token_settings)}"})

    assert (response.status_code, events) == (expected[0], [expected[1], "response started"])


@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("GET", "/policies/not-a-uuid", None), ("PATCH", "/documents/1", {"original_filename": 1}),
     ("POST", "/policies", POLICY | {"company_id": str(uuid.uuid4()), "insurance_type": "LIFE"}),
     ("PUT", f"/documents/{ID}/status", {"status": "ACTIVE", "storage_reference": "x"})],
    ids=["bad-id", "bad-id-and-body", "bad-body-with-company", "bad-status-with-storage"],
)
def test_anonymous_requests_are_401_before_any_validation(
    api: TestClient, opened: list[str], method: str, path: str, body: Any
) -> None:
    # An unauthenticated client learns nothing about ids or body rules: authentication comes first. (A body that is
    # not JSON at all is the one exception: FastAPI parses JSON before any dependency, see SECURITY.md §14.)
    response = _send(api, method, path, body)

    assert (response.status_code, response.json()) == (401, {"detail": "Not authenticated"})
    assert opened == []


@pytest.mark.security
@pytest.mark.parametrize("path", ["/policies", f"/policies/{ID}/documents"])
def test_a_body_that_is_not_json_is_rejected_before_authentication_without_reaching_anything(
    api: TestClient, opened: list[str], path: str
) -> None:
    # The one response an anonymous client gets before authentication: FastAPI parses JSON before dependencies.
    # It carries only the parser's error about the client's own bytes, and reaches no identity, tenant or database.
    response = api.post(path, content=b'{"company_id": "x", not json', headers={"Content-Type": "application/json"})

    assert response.status_code == 422
    assert {item["type"] for item in response.json()["detail"]} == {"json_invalid"}
    assert opened == []
