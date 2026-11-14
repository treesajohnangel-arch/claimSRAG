"""Unit tests for tenant-aware database access (no PostgreSQL, no network).

A recording fake connection captures each SQL statement and its parameters, so these tests show what would be sent
to the database: the tenant is always a bound parameter taken from the TenantIdentity, and an invalid tenant is
refused before any query. tests/database/test_tenant_db.py runs the same boundary against PostgreSQL.
"""

import ast
import inspect
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.tenant_db as tenant_db_module
from app.auth import AuthenticatedUser
from app.config import Settings
from app.logging_config import configure_logging
from app.models import DocumentStatus, InsuranceType, PolicyStatus, UserRole, UserStatus
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TENANT_SCOPE_FAILURE_MESSAGE, TenantDatabase, TenantScopeError, TenantUser
from app.tokens import TokenIdentity

COMPANY_A, COMPANY_B = uuid.uuid4(), uuid.uuid4()
TENANT_A = TenantIdentity(company_id=COMPANY_A)


class _RecordingConnection:
    """Stands in for a psycopg connection: records every query and returns the rows it was given."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def cursor(self, row_factory: Any = None) -> "_RecordingConnection":
        return self

    def __enter__(self) -> "_RecordingConnection":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, query: str, params: dict[str, Any]) -> "_RecordingConnection":
        self.calls.append((query, params))
        return self

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


def _row(company_id: uuid.UUID, email: str = "a1@a.example") -> dict[str, Any]:
    return {"id": uuid.uuid4(), "company_id": company_id, "email": email, "full_name": "User", "role": "STAFF",
            "status": "ACTIVE"}


# --- A valid tenant -------------------------------------------------------------------------------------------


def test_valid_tenant_is_accepted_and_exposed_read_only() -> None:
    tenant_db = TenantDatabase(_RecordingConnection(), TENANT_A)

    assert tenant_db.tenant is TENANT_A


@pytest.mark.security
def test_every_query_binds_the_tenants_company_id_as_a_parameter() -> None:
    conn = _RecordingConnection([_row(COMPANY_A)])
    tenant_db = TenantDatabase(conn, TENANT_A)

    users = tenant_db.list_users()
    tenant_db.get_user(uuid.uuid4())

    assert [user.company_id for user in users] == [COMPANY_A]
    assert len(conn.calls) == 2
    for query, params in conn.calls:
        assert "WHERE company_id = %(company_id)s" in query
        assert params["company_id"] is COMPANY_A  # the TenantIdentity's own UUID object, bound, never formatted
        assert str(COMPANY_A) not in query
        assert "password_hash" not in query


@pytest.mark.security
def test_a_parameter_named_company_id_cannot_replace_the_tenant() -> None:
    conn = _RecordingConnection()

    TenantDatabase(conn, TENANT_A)._fetch("SELECT 1 WHERE %(company_id)s IS NOT NULL", {"company_id": COMPANY_B})

    assert conn.calls[0][1]["company_id"] is COMPANY_A


# --- Fail closed ----------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "tenant",
    [
        None,
        COMPANY_A,
        str(COMPANY_A),
        {"company_id": str(COMPANY_A)},
        SimpleNamespace(company_id=COMPANY_A),
        TokenIdentity(user_id=uuid.uuid4(), company_id=COMPANY_A, role=UserRole.COMPANY_ADMIN),
        AuthenticatedUser(user_id=uuid.uuid4(), company_id=COMPANY_A, email="a@a.example", full_name="A",
                          role=UserRole.COMPANY_ADMIN, status=UserStatus.ACTIVE),
        TenantIdentity.model_construct(),
        TenantIdentity.model_construct(company_id=None),
        TenantIdentity.model_construct(company_id=str(COMPANY_B)),
        TenantIdentity.model_construct(company_id=uuid.UUID(int=0)),
    ],
    ids=["none", "bare-uuid", "uuid-string", "request-payload", "look-alike-object", "token-identity",
         "authenticated-user", "tenant-without-company", "tenant-null-company", "tenant-string-company",
         "tenant-nil-company"],
)
def test_missing_or_invalid_tenant_is_refused_before_any_query(tenant: object) -> None:
    conn = _RecordingConnection([_row(COMPANY_B)])

    with pytest.raises(TenantScopeError) as excinfo:
        TenantDatabase(conn, tenant)  # type: ignore[arg-type]

    assert conn.calls == []
    assert str(excinfo.value) == TENANT_SCOPE_FAILURE_MESSAGE


@pytest.mark.security
def test_rows_outside_the_tenant_are_never_returned() -> None:
    # Defence in depth: if a query ever returned another tenant's row, the access layer refuses it.
    listing = TenantDatabase(_RecordingConnection([_row(COMPANY_A), _row(COMPANY_B)]), TENANT_A)
    lookup = TenantDatabase(_RecordingConnection([_row(COMPANY_B)]), TENANT_A)

    with pytest.raises(TenantScopeError):
        listing.list_users()
    with pytest.raises(TenantScopeError):
        lookup.get_user(uuid.uuid4())


# --- No caller-selected tenant --------------------------------------------------------------------------------


@pytest.mark.security
def test_no_method_accepts_a_company_id() -> None:
    tenant_db = TenantDatabase(_RecordingConnection(), TENANT_A)

    assert list(inspect.signature(TenantDatabase).parameters) == ["conn", "tenant"]
    assert list(inspect.signature(tenant_db.list_users).parameters) == []
    assert list(inspect.signature(tenant_db.get_user).parameters) == ["user_id"]
    with pytest.raises(TypeError):
        tenant_db.list_users(company_id=COMPANY_B)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_db.get_user(uuid.uuid4(), company_id=COMPANY_B)  # type: ignore[call-arg]


@pytest.mark.security
def test_tenant_cannot_be_changed_after_construction() -> None:
    tenant_db = TenantDatabase(_RecordingConnection(), TENANT_A)

    for name in ("_tenant", "tenant", "_conn", "company_id"):
        with pytest.raises(AttributeError):
            setattr(tenant_db, name, TenantIdentity(company_id=COMPANY_B))

    assert tenant_db.tenant.company_id == COMPANY_A


@pytest.mark.security
@pytest.mark.parametrize("user_id", ["' OR '1'='1", str(uuid.uuid4()), 1, None], ids=["injection", "uuid-string",
                                                                                       "int", "none"])
def test_get_user_takes_only_a_uuid(user_id: object) -> None:
    conn = _RecordingConnection()

    with pytest.raises(TypeError):
        TenantDatabase(conn, TENANT_A).get_user(user_id)  # type: ignore[arg-type]

    assert conn.calls == []


@pytest.mark.security
def test_role_cannot_widen_the_scope() -> None:
    # The access layer has no role input. A COMPANY_ADMIN's tenant is its own company, exactly like STAFF's.
    conns = {role: _RecordingConnection() for role in UserRole}
    for role, conn in conns.items():
        identity = TokenIdentity(user_id=uuid.uuid4(), company_id=COMPANY_A, role=role)
        TenantDatabase(conn, resolve_tenant(identity)).list_users()

    assert {conn.calls[0][1]["company_id"] for conn in conns.values()} == {COMPANY_A}


# --- What the layer knows and holds ---------------------------------------------------------------------------


@pytest.mark.security
def test_access_object_holds_only_the_connection_and_the_tenant() -> None:
    tenant_db = TenantDatabase(_RecordingConnection(), TENANT_A)

    assert TenantDatabase.__slots__ == ("_conn", "_tenant")
    assert not hasattr(tenant_db, "__dict__")
    assert set(TenantIdentity.model_fields) == {"company_id"}
    assert set(TenantUser.model_fields) == {"id", "company_id", "email", "full_name", "role", "status"}


@pytest.mark.security
def test_database_layer_does_not_handle_http_tokens_or_permissions() -> None:
    # Checked on the parsed code, so docstrings and comments that describe the boundary do not count.
    tree = ast.parse(Path(tenant_db_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    used = attributes | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    # Only the standard library, the driver, pydantic, the row enums and the tenant type: no web framework, no JWT
    # library, no token or RBAC module.
    assert imported == {"logging", "typing", "uuid", "psycopg", "psycopg.rows", "pydantic", "app.models", "app.tenancy"}
    for name in ("decode_access_token", "Request", "headers", "cookies", "query_params", "require_permission",
                 "has_permission"):
        assert name not in used
    # No identity's role or user id is ever read (`role` is only a column of the TenantUser row model).
    assert not {"role", "user_id"} & attributes


@pytest.mark.security
def test_refusal_is_logged_without_tenant_values(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(settings)

    with pytest.raises(TenantScopeError):
        TenantDatabase(_RecordingConnection(), {"company_id": str(COMPANY_B)})  # type: ignore[arg-type]
    logs = capsys.readouterr().err

    assert "tenant-scoped database access refused: no valid tenant" in logs
    assert str(COMPANY_B) not in logs


# --- Phase 2.1: policies and policy documents -----------------------------------------------------------------


def _policy_row(company_id: uuid.UUID) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"id": uuid.uuid4(), "company_id": company_id, "policy_number": "POL-001", "name": "Health Cover",
            "insurance_type": "HEALTH", "status": "ACTIVE", "created_at": now, "updated_at": now}


def _document_row(company_id: uuid.UUID) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {"id": uuid.uuid4(), "company_id": company_id, "policy_id": uuid.uuid4(), "document_key": "policy-wording",
            "version": 1, "status": "UPLOADED", "original_filename": "p.pdf", "storage_reference": "docs/p.pdf",
            "content_type": "application/pdf", "size_bytes": 10, "checksum_sha256": "0" * 64, "created_at": now,
            "updated_at": now}


@pytest.mark.security
def test_policy_and_document_queries_bind_the_tenants_company_id_as_a_parameter() -> None:
    conn = _RecordingConnection()
    tenant_db = TenantDatabase(conn, TENANT_A)
    policy_id, document_id = uuid.uuid4(), uuid.uuid4()

    tenant_db.list_policies()
    tenant_db.get_policy(policy_id)
    tenant_db.list_documents(policy_id)
    tenant_db.get_document(document_id)

    assert len(conn.calls) == 4
    for query, params in conn.calls:
        assert "WHERE company_id = %(company_id)s" in query
        assert params["company_id"] is COMPANY_A
        assert str(COMPANY_A) not in query and str(policy_id) not in query and str(document_id) not in query
    assert [params for _, params in conn.calls][1:] == [
        {"policy_id": policy_id, "company_id": COMPANY_A},
        {"policy_id": policy_id, "company_id": COMPANY_A},
        {"document_id": document_id, "company_id": COMPANY_A},
    ]


@pytest.mark.security
@pytest.mark.parametrize(
    ("method", "row"),
    [("list_policies", _policy_row), ("get_policy", _policy_row), ("list_documents", _document_row),
     ("get_document", _document_row)],
)
def test_policy_and_document_rows_outside_the_tenant_are_never_returned(method: str, row: Any) -> None:
    # Defence in depth, as for users: a listing with one stray row, or a lookup answered by another tenant's row.
    rows = [row(COMPANY_A), row(COMPANY_B)] if method.startswith("list") else [row(COMPANY_B)]
    tenant_db = TenantDatabase(_RecordingConnection(rows), TENANT_A)
    call = getattr(tenant_db, method)

    with pytest.raises(TenantScopeError):
        call() if method == "list_policies" else call(uuid.uuid4())


@pytest.mark.security
@pytest.mark.parametrize("method", ["get_policy", "list_documents", "get_document"])
@pytest.mark.parametrize("value", ["' OR '1'='1", str(uuid.uuid4()), 1, None], ids=["injection", "uuid-string", "int",
                                                                                    "none"])
def test_policy_and_document_lookups_take_only_a_uuid(method: str, value: object) -> None:
    conn = _RecordingConnection()

    with pytest.raises(TypeError):
        getattr(TenantDatabase(conn, TENANT_A), method)(value)
    assert conn.calls == []


@pytest.mark.security
def test_no_tenant_database_method_accepts_a_company_tenant_or_role() -> None:
    # Covers every public method, so a future one that takes a tenant from its caller fails here.
    forbidden = {"company_id", "company", "tenant", "tenant_id", "role", "roles"}
    methods = {name: member for name, member in inspect.getmembers(TenantDatabase, inspect.isfunction)
               if not name.startswith("_")}

    assert set(methods) == {"list_users", "get_user", "list_policies", "get_policy", "list_documents", "get_document",
                            "create_policy", "update_policy", "set_policy_status", "delete_policy",
                            "create_document", "update_document", "set_document_status", "delete_document",
                            "insert_chunk_embedding", "list_chunk_embeddings", "get_chunk_embedding",
                            "delete_chunk_embedding", "search_chunks_by_vector", "search_chunks_by_keywords"}
    for name, method in methods.items():
        assert not forbidden & set(inspect.signature(method).parameters), name


# --- Phase 2.2: policy writes ---------------------------------------------------------------------------------


@pytest.mark.security
def test_policy_writes_bind_the_tenant_and_never_format_values_into_sql() -> None:
    conn = _RecordingConnection([_policy_row(COMPANY_A)])
    tenant_db = TenantDatabase(conn, TENANT_A)
    policy_id = uuid.uuid4()

    tenant_db.create_policy(policy_number="POL-9", name="Cover", insurance_type=InsuranceType.MOTOR)
    tenant_db.update_policy(policy_id, name="Renamed")
    tenant_db.set_policy_status(policy_id, PolicyStatus.INACTIVE)
    tenant_db.delete_policy(policy_id)

    (insert, created), (update, updated), (status_sql, status), (delete, deleted) = conn.calls
    assert insert.split()[:5] == ["INSERT", "INTO", "policies", "(company_id,", "policy_number,"]
    assert created == {"policy_number": "POL-9", "name": "Cover", "insurance_type": "MOTOR", "status": "ACTIVE",
                       "company_id": COMPANY_A}
    assert updated == {"policy_id": policy_id, "policy_number": None, "name": "Renamed", "insurance_type": None,
                       "company_id": COMPANY_A}
    assert status == {"policy_id": policy_id, "status": "INACTIVE", "company_id": COMPANY_A}
    assert deleted == {"policy_id": policy_id, "company_id": COMPANY_A}
    for query in (update, status_sql, delete):
        assert "WHERE company_id = %(company_id)s AND id = %(policy_id)s" in query
    for query, _ in conn.calls:
        assert str(COMPANY_A) not in query and str(policy_id) not in query and "POL-9" not in query


@pytest.mark.security
@pytest.mark.parametrize(
    "write",
    [
        lambda t: t.create_policy(policy_number="P-1", name="N", insurance_type="HEALTH"),
        lambda t: t.create_policy(policy_number="P-1", name="N", insurance_type=InsuranceType.HOME, status="ACTIVE"),
        lambda t: t.create_policy(policy_number=1, name="N", insurance_type=InsuranceType.HOME),
        lambda t: t.create_policy(policy_number="P-1", name=None, insurance_type=InsuranceType.HOME),
        lambda t: t.update_policy(str(uuid.uuid4()), name="N"),
        lambda t: t.update_policy(uuid.uuid4(), insurance_type="TRAVEL"),
        lambda t: t.update_policy(uuid.uuid4(), policy_number=7),
        lambda t: t.update_policy(uuid.uuid4()),
        lambda t: t.set_policy_status(uuid.uuid4(), "INACTIVE"),
        lambda t: t.set_policy_status("' OR '1'='1", PolicyStatus.ACTIVE),
        lambda t: t.delete_policy(str(uuid.uuid4())),
        lambda t: t.delete_policy(None),
    ],
    ids=["create-type-string", "create-status-string", "create-number-int", "create-name-none", "update-id-string",
         "update-type-string", "update-number-int", "update-nothing", "status-string", "status-injection-id",
         "delete-id-string", "delete-id-none"],
)
def test_policy_writes_reject_untyped_input_before_any_sql(write: Any) -> None:
    conn = _RecordingConnection([_policy_row(COMPANY_A)])

    with pytest.raises((TypeError, ValueError)):
        write(TenantDatabase(conn, TENANT_A))
    assert conn.calls == []


@pytest.mark.security
@pytest.mark.parametrize(
    "write",
    [
        lambda t: t.create_policy(policy_number="P-1", name="N", insurance_type=InsuranceType.HOME),
        lambda t: t.update_policy(uuid.uuid4(), name="N"),
        lambda t: t.set_policy_status(uuid.uuid4(), PolicyStatus.ACTIVE),
        lambda t: t.delete_policy(uuid.uuid4()),
    ],
    ids=["create", "update", "status", "delete"],
)
def test_a_write_answered_with_another_tenants_row_is_refused(write: Any) -> None:
    # Defence in depth: the scoped SQL cannot return such a row, but if it ever did, the caller gets an error.
    with pytest.raises(TenantScopeError):
        write(TenantDatabase(_RecordingConnection([_policy_row(COMPANY_B)]), TENANT_A))


@pytest.mark.security
@pytest.mark.parametrize(
    "write",
    [
        lambda t: t.create_policy(policy_number="P-1", name="N", insurance_type=InsuranceType.HOME,
                                  company_id=COMPANY_B),
        lambda t: t.update_policy(uuid.uuid4(), name="N", company_id=COMPANY_B),
        lambda t: t.update_policy(uuid.uuid4(), id=uuid.uuid4()),
        lambda t: t.update_policy(uuid.uuid4(), status=PolicyStatus.INACTIVE),
        lambda t: t.set_policy_status(uuid.uuid4(), PolicyStatus.ACTIVE, company_id=COMPANY_B),
        lambda t: t.delete_policy(uuid.uuid4(), company_id=COMPANY_B),
    ],
    ids=["create-company", "update-company", "update-id", "update-status", "status-company", "delete-company"],
)
def test_policy_writes_have_no_company_id_or_primary_key_parameter(write: Any) -> None:
    conn = _RecordingConnection([_policy_row(COMPANY_A)])

    with pytest.raises(TypeError):
        write(TenantDatabase(conn, TENANT_A))
    assert conn.calls == []


# --- Phase 2.3: policy-document writes ------------------------------------------------------------------------


def _document_metadata(**changes: Any) -> dict[str, Any]:
    return {"document_key": "policy-wording", "version": 1, "original_filename": "p.pdf",
            "storage_reference": "docs/p.pdf", "content_type": "application/pdf", "size_bytes": 10,
            "checksum_sha256": "0" * 64, **changes}


@pytest.mark.security
def test_document_writes_bind_the_tenant_and_never_format_values_into_sql() -> None:
    conn = _RecordingConnection([_document_row(COMPANY_A)])
    tenant_db = TenantDatabase(conn, TENANT_A)
    policy_id, document_id = uuid.uuid4(), uuid.uuid4()

    tenant_db.create_document(policy_id, **_document_metadata())
    tenant_db.update_document(document_id, original_filename="renamed.pdf")
    tenant_db.set_document_status(document_id, DocumentStatus.INACTIVE)
    tenant_db.delete_document(document_id)

    (insert, created), (update, updated), (status_sql, status), (delete, deleted) = conn.calls
    # The company is the first column and the policy sits beside it, so the composite foreign key checks the pair.
    assert insert.split()[:5] == ["INSERT", "INTO", "policy_documents", "(company_id,", "policy_id,"]
    assert "VALUES (%(company_id)s, %(policy_id)s," in " ".join(insert.split())
    assert created == {"policy_id": policy_id, **_document_metadata(), "status": "UPLOADED", "company_id": COMPANY_A}
    assert updated == {"document_id": document_id, "original_filename": "renamed.pdf", "company_id": COMPANY_A}
    assert status == {"document_id": document_id, "status": "INACTIVE", "company_id": COMPANY_A}
    assert deleted == {"document_id": document_id, "company_id": COMPANY_A}
    for query in (update, status_sql, delete):
        assert "WHERE company_id = %(company_id)s AND id = %(document_id)s" in query
    for query, _ in conn.calls:
        assert str(COMPANY_A) not in query and str(policy_id) not in query and str(document_id) not in query
        assert "docs/p.pdf" not in query and "renamed.pdf" not in query


@pytest.mark.security
@pytest.mark.parametrize(
    "write",
    [
        lambda t: t.create_document(str(uuid.uuid4()), **_document_metadata()),
        lambda t: t.create_document(None, **_document_metadata()),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(version="1")),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(version=True)),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(size_bytes=1.5)),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(document_key=None)),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(checksum_sha256=b"0" * 64)),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(storage_reference=["docs/p.pdf"])),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(), status="UPLOADED"),
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata(), status=PolicyStatus.INACTIVE),
        lambda t: t.update_document(str(uuid.uuid4()), original_filename="p.pdf"),
        lambda t: t.update_document(uuid.uuid4(), original_filename=None),
        lambda t: t.set_document_status(uuid.uuid4(), "INACTIVE"),
        lambda t: t.set_document_status(uuid.uuid4(), PolicyStatus.INACTIVE),
        lambda t: t.set_document_status("' OR '1'='1", DocumentStatus.UPLOADED),
        lambda t: t.delete_document(str(uuid.uuid4())),
        lambda t: t.delete_document(None),
    ],
    ids=["create-policy-id-string", "create-policy-id-none", "create-version-string", "create-version-bool",
         "create-size-float", "create-key-none", "create-checksum-bytes", "create-storage-list", "create-status-string",
         "create-status-policy-enum", "update-id-string", "update-filename-none", "status-string",
         "status-policy-enum", "status-injection-id", "delete-id-string", "delete-id-none"],
)
def test_document_writes_reject_untyped_input_before_any_sql(write: Any) -> None:
    conn = _RecordingConnection([_document_row(COMPANY_A)])

    with pytest.raises(TypeError):
        write(TenantDatabase(conn, TENANT_A))
    assert conn.calls == []


@pytest.mark.security
@pytest.mark.parametrize(
    "write",
    [
        lambda t: t.create_document(uuid.uuid4(), **_document_metadata()),
        lambda t: t.update_document(uuid.uuid4(), original_filename="p.pdf"),
        lambda t: t.set_document_status(uuid.uuid4(), DocumentStatus.INACTIVE),
        lambda t: t.delete_document(uuid.uuid4()),
    ],
    ids=["create", "update", "status", "delete"],
)
def test_a_document_write_answered_with_another_tenants_row_is_refused(write: Any) -> None:
    # Defence in depth: the scoped SQL cannot return such a row, but if it ever did, the caller gets an error.
    with pytest.raises(TenantScopeError):
        write(TenantDatabase(_RecordingConnection([_document_row(COMPANY_B)]), TENANT_A))


@pytest.mark.security
def test_document_writes_take_no_company_and_never_change_a_versions_identity_or_file() -> None:
    # D-025: only the display file name (update_document) and the status (set_document_status) change in place.
    conn = _RecordingConnection([_document_row(COMPANY_A)])
    tenant_db = TenantDatabase(conn, TENANT_A)
    foreign = _document_row(COMPANY_B) | {"policy_id": uuid.uuid4(), "status": DocumentStatus.INACTIVE}

    assert list(inspect.signature(tenant_db.update_document).parameters) == ["document_id", "original_filename"]
    assert list(inspect.signature(tenant_db.set_document_status).parameters) == ["document_id", "status"]
    assert list(inspect.signature(tenant_db.delete_document).parameters) == ["document_id"]
    assert not {"company_id", "id"} & set(inspect.signature(tenant_db.create_document).parameters)
    for field in ("company_id", "id", "policy_id", "document_key", "version", "storage_reference", "content_type",
                  "size_bytes", "checksum_sha256", "status"):
        with pytest.raises(TypeError):
            tenant_db.update_document(uuid.uuid4(), original_filename="p.pdf", **{field: foreign[field]})
    with pytest.raises(TypeError):
        tenant_db.create_document(uuid.uuid4(), **_document_metadata(), company_id=COMPANY_B)
    with pytest.raises(TypeError):
        tenant_db.create_document(uuid.uuid4(), **_document_metadata(), id=uuid.uuid4())
    with pytest.raises(TypeError):
        tenant_db.set_document_status(uuid.uuid4(), DocumentStatus.UPLOADED, company_id=COMPANY_B)
    with pytest.raises(TypeError):
        tenant_db.delete_document(uuid.uuid4(), company_id=COMPANY_B)
    assert conn.calls == []
