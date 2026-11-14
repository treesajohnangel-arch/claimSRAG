"""Structural security checks on the application source (Phase 1.8). No application instance, no database.

Each test parses every module under app/ (code only: docstrings and comments do not count) and pins one rule of the
identity → authorization → tenant → database boundary, so a future edit that breaks the rule fails here even before
a behavioural test notices:

- tokens are verified in one place only (``current_identity``);
- ``TokenIdentity`` is built only from a verified token, ``TenantIdentity`` only by ``resolve_tenant``;
- application code reads no client request data (no query, path, header, cookie or body access);
- every SQL statement on a tenant-owned table is tenant-scoped, and the tenant layer runs SQL only through ``_fetch``;
- no code compares roles (permissions come only from ROLE_PERMISSIONS);
- the boundary functions accept no role or company argument.

Phase 1.9 adds:

- no identity is copied with changed fields or built without validation (``model_copy(update=...)``,
  ``model_construct``), which would bypass the single construction sites above;
- no behaviour outside configuration depends on ENVIRONMENT or DEBUG, so the boundary the tests verify
  (ENVIRONMENT=test) is the one that runs in development and production;
- only the login lookup reads ``password_hash``.
"""

import ast
import inspect
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.api.authorization import permission_required
from app.api.tenancy import current_tenant
from app.config import BACKEND_DIR
from app.rbac import Permission, has_permission, require_permission
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase

APP_DIR = BACKEND_DIR / "app"
# Tables whose rows belong to a tenant. Add each new tenant-owned table here.
TENANT_OWNED_TABLES = ("users", "policies", "policy_documents", "chunk_embeddings")
TENANT_SCOPE = "company_id = %(company_id)s"
SQL_ON_TABLE = re.compile(rf"\b(?:FROM|JOIN|UPDATE|INTO)\s+(?:{'|'.join(TENANT_OWNED_TABLES)})\b", re.IGNORECASE)
# Phase 2.2: an INSERT is tenant-scoped when company_id is its first column and the bound tenant its first value.
INSERT_SCOPE = re.compile(r"INSERT\s+INTO\s+\w+\s*\(\s*company_id\s*,[^)]*\)\s*VALUES\s*\(\s*%\(company_id\)s\s*,",
                          re.IGNORECASE)
# Phase 2.3: an UPDATE or DELETE names exactly one row of the tenant. Its whole WHERE clause is the tenant filter AND
# the row's id: no OR, no wider filter.
WRITE_SCOPE = re.compile(r"company_id = %\(company_id\)s AND id = %\(\w+_id\)s", re.IGNORECASE)
# No SET assigns these: a row never changes tenant, identity or parent policy.
FIXED_COLUMNS = frozenset({"company_id", "id", "policy_id"})
# A child row is inserted with its parent key right beside company_id, both bound, so the composite foreign key
# (company_id, parent) → parent (company_id, id) checks the parent against the tenant (D-024).
PARENT_KEYS = {"policy_documents": "policy_id", "chunk_embeddings": "policy_id"}

pytestmark = pytest.mark.security


def _modules() -> Iterator[tuple[str, ast.Module]]:
    for path in sorted(APP_DIR.rglob("*.py")):
        yield path.relative_to(BACKEND_DIR).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def _docstrings(tree: ast.Module) -> set[int]:
    nodes = [tree, *(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))]
    return {id(node.body[0].value) for node in nodes
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        if isinstance(call.func.value, ast.Name):
            return f"{call.func.value.id}.{call.func.attr}"
        return call.func.attr
    return None


def _calls(name_prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    """Module → the calls in it whose name starts with one of the prefixes."""
    found: dict[str, list[str]] = {}
    for module, tree in _modules():
        for node in ast.walk(tree):
            name = _called_name(node) if isinstance(node, ast.Call) else None
            if name and name.startswith(name_prefixes):
                found.setdefault(module, []).append(name)
    return found


def test_tokens_are_verified_only_by_current_identity() -> None:
    assert _calls(("decode_access_token",)) == {"app/api/authorization.py": ["decode_access_token"]}


def test_token_identity_is_built_only_from_a_verified_token() -> None:
    assert _calls(("TokenIdentity",)) == {"app/tokens.py": ["TokenIdentity"]}


def test_tenant_identity_is_built_only_by_resolve_tenant() -> None:
    assert _calls(("TenantIdentity",)) == {"app/tenancy.py": ["TenantIdentity"]}
    assert _calls(("resolve_tenant",)) == {"app/api/tenancy.py": ["resolve_tenant"]}


def test_application_code_reads_no_client_request_data() -> None:
    # The only client input the boundary uses is the bearer credential, read by FastAPI's HTTPBearer for
    # current_identity. Request logging reads the method and URL path only.
    request_data = {"query_params", "path_params", "cookies", "headers", "json", "body", "form", "stream"}
    found = {
        (module, node.attr)
        for module, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in request_data
    }

    assert found == set()


def _tenant_scoped(sql: str) -> bool:
    """An INSERT stores the bound tenant as the new row's company_id, with a child row's parent key beside it. An
    UPDATE or DELETE filters on exactly the bound tenant and one row id. A SELECT filters on the bound tenant in its
    WHERE clause, with no OR. No statement assigns company_id, id or policy_id, and anything else (a CTE, say) is
    refused."""
    words = sql.split(None, 1)
    kind = words[0].upper() if words else ""
    if kind == "INSERT":
        table = re.match(r"\s*INSERT\s+INTO\s+(\w+)", sql, re.IGNORECASE)
        parent = PARENT_KEYS.get(table.group(1).lower()) if table else None
        paired = parent is None or re.search(
            rf"\(\s*company_id\s*,\s*{parent}\s*,[^)]*\)\s*VALUES\s*\(\s*%\(company_id\)s\s*,\s*%\({parent}\)s\s*,",
            sql, re.IGNORECASE) is not None
        return INSERT_SCOPE.search(sql) is not None and paired
    if kind not in {"SELECT", "UPDATE", "DELETE"}:
        return False
    parts = re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.IGNORECASE)
    if len(parts) != 2:
        return False
    before_where, where = parts
    assignment = re.search(r"\bSET\b(.*)", before_where, re.IGNORECASE | re.DOTALL)
    if assignment and ("company_id" in assignment.group(1)
                       or FIXED_COLUMNS & {column.lower() for column in re.findall(r"(\w+)\s*=", assignment.group(1))}):
        return False
    if kind in {"UPDATE", "DELETE"}:
        condition = " ".join(re.split(r"\bRETURNING\b", where, flags=re.IGNORECASE)[0].split())
        return WRITE_SCOPE.fullmatch(condition) is not None
    return TENANT_SCOPE in where and re.search(r"\bOR\b", where, re.IGNORECASE) is None


def _tenant_statements() -> list[tuple[str, str]]:
    """(module, SQL) for every string constant in app/ that reads or writes a tenant-owned table."""
    return [
        (module, node.value)
        for module, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in _docstrings(tree)
        and SQL_ON_TABLE.search(node.value)
    ]


def test_every_statement_on_a_tenant_owned_table_is_scoped() -> None:
    statements = _tenant_statements()

    assert statements, "the scan must find the existing statements"
    exceptions = []
    for module, sql in statements:
        if module == "app/tenant_db.py":
            assert _tenant_scoped(sql), sql
        elif module == "app/auth.py":
            # Pre-authentication exception 1: login, and the demo session's lookup of the configured demo user
            # (D-036), look the user up within one company, by slug. Neither has a company id parameter.
            assert "c.slug = %(slug)s" in sql and "%(company_id)s" not in sql, (module, sql)
            exceptions.append(module)
        else:
            # Pre-authentication exception 2 (D-035): registration creates a company and its first admin in ONE
            # statement. The user's company_id is the id generated for the new company in that same statement, never a
            # parameter, and the role is the literal COMPANY_ADMIN.
            assert module == "app/registration.py", (module, sql)
            compact = " ".join(sql.split())
            assert compact.startswith("WITH company AS ( INSERT INTO companies (name, slug) VALUES "), compact
            assert "INSERT INTO users (company_id, email, password_hash, full_name, role) SELECT company.id, " in compact
            assert "'COMPANY_ADMIN' FROM company" in compact and "%(company_id)s" not in compact
            assert "%(role)s" not in compact and "password_hash" not in compact.rsplit("RETURNING", 1)[1]  # final
            exceptions.append(module)
    assert sorted(exceptions) == ["app/auth.py", "app/auth.py", "app/registration.py"]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO policies (company_id, name) VALUES (%(client_company_id)s, %(name)s)",
        "INSERT INTO policies (name, company_id) VALUES (%(name)s, %(company_id)s)",
        "UPDATE policies SET name = %(name)s WHERE id = %(policy_id)s",
        "UPDATE policies SET company_id = %(company_id)s WHERE company_id = %(company_id)s AND id = %(policy_id)s",
        "UPDATE policies SET name = %(name)s, company_id = %(company_id)s WHERE id = %(policy_id)s",
        "DELETE FROM policies WHERE id = %(policy_id)s",
        "SELECT id FROM policies",
        "SELECT company_id = %(company_id)s FROM policies WHERE id = %(policy_id)s",
    ],
    ids=["insert-client-company", "insert-company-not-first", "update-unscoped", "update-moves-tenant",
         "update-scope-only-in-set", "delete-unscoped", "select-no-where", "scope-outside-where"],
)
def test_the_scope_rule_rejects_unscoped_or_tenant_changing_statements(sql: str) -> None:
    # Phase 2.2 positive control: the rule above recognises each unsafe shape of statement.
    assert not _tenant_scoped(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO policy_documents (company_id, document_key, policy_id) "
        "VALUES (%(company_id)s, %(document_key)s, %(policy_id)s)",
        "INSERT INTO policy_documents (company_id, policy_id, version) "
        "SELECT company_id, id, %(version)s FROM policies WHERE id = %(policy_id)s",
        "UPDATE policy_documents SET status = %(status)s WHERE company_id = %(company_id)s OR id = %(document_id)s",
        "UPDATE policy_documents SET status = %(status)s "
        "WHERE company_id = %(company_id)s AND id = %(document_id)s OR true",
        "UPDATE policy_documents SET policy_id = %(policy_id)s "
        "WHERE company_id = %(company_id)s AND id = %(document_id)s",
        "UPDATE policy_documents SET id = %(new_id)s WHERE company_id = %(company_id)s AND id = %(document_id)s",
        "DELETE FROM policy_documents WHERE company_id = %(company_id)s",
        "SELECT id FROM policy_documents WHERE company_id = %(company_id)s OR id = %(document_id)s",
        "WITH gone AS (DELETE FROM policy_documents WHERE id = %(document_id)s RETURNING *) "
        "SELECT * FROM gone WHERE company_id = %(company_id)s",
    ],
    ids=["insert-policy-not-beside-company", "insert-company-from-a-policy-lookup", "update-or-instead-of-and",
         "update-trailing-or", "update-moves-policy", "update-changes-id", "delete-whole-tenant", "select-or",
         "cte-unscoped-delete"],
)
def test_the_scope_rule_rejects_unsafe_document_writes(sql: str) -> None:
    # Phase 2.3 positive control, for the rules added with the document writes.
    assert not _tenant_scoped(sql)


def test_the_document_writes_are_among_the_scanned_statements() -> None:
    # The scan above checks them only if it finds them: all four document writes live in the tenant layer.
    kinds = {" ".join(sql.split()[:3]) for module, sql in _tenant_statements() if module == "app/tenant_db.py"}

    assert {"INSERT INTO policy_documents", "UPDATE policy_documents SET", "DELETE FROM policy_documents"} <= kinds


def test_tenant_layer_runs_sql_only_through_fetch() -> None:
    tree = ast.parse((APP_DIR / "tenant_db.py").read_text(encoding="utf-8"))
    executing = {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef)
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"execute", "executemany", "copy", "cursor"}
    }

    # _fetch binds the TenantIdentity's company id to every statement; nothing else touches the connection.
    assert executing == {"_fetch"}


def test_no_code_compares_roles() -> None:
    def is_role(node: ast.AST) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "role") or (
            isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "UserRole"
        ) or (isinstance(node, ast.Constant) and node.value in {"COMPANY_ADMIN", "STAFF"})

    comparisons = [
        (module, node.lineno)
        for module, tree in _modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare) and any(is_role(side) for side in (node.left, *node.comparators))
    ]

    assert comparisons == []


def test_boundary_functions_accept_no_role_or_company_argument() -> None:
    client_names = {"role", "roles", "company_id", "tenant_id", "permissions"}
    signatures = {
        "require_permission": inspect.signature(require_permission),
        "has_permission": inspect.signature(has_permission),
        "permission_required(...) check": inspect.signature(permission_required(Permission.POLICY_READ)),
        "resolve_tenant": inspect.signature(resolve_tenant),
        "current_tenant": inspect.signature(current_tenant),
        "TenantDatabase": inspect.signature(TenantDatabase),
        "TenantDatabase.list_users": inspect.signature(TenantDatabase.list_users),
        "TenantDatabase.get_user": inspect.signature(TenantDatabase.get_user),
    }

    for name, signature in signatures.items():
        assert not client_names & set(signature.parameters), name
    assert signatures["TenantDatabase"].parameters["tenant"].annotation is TenantIdentity


# --- Phase 1.9 -------------------------------------------------------------------------------------------------

IDENTITY_FIELDS = {"user_id", "company_id", "role"}


def test_identities_are_never_copied_with_changes_or_built_without_validation() -> None:
    # A frozen TokenIdentity or TenantIdentity can still be copied with other values (model_copy(update=...), which
    # also skips validation) or built unvalidated (model_construct). Either would create an identity outside the
    # single construction sites pinned above. A model_copy update is allowed only as a literal dict that sets no
    # identity field.
    def unsafe(call: ast.Call) -> bool:
        name = _called_name(call) or ""
        if name.endswith("model_construct"):
            return True
        if not name.endswith("model_copy"):
            return False
        update = next((keyword.value for keyword in call.keywords if keyword.arg == "update"), None)
        if update is None:
            return False
        return not (isinstance(update, ast.Dict) and all(
            isinstance(key, ast.Constant) and key.value not in IDENTITY_FIELDS for key in update.keys))

    found = [(module, node.lineno) for module, tree in _modules() for node in ast.walk(tree)
             if isinstance(node, ast.Call) and unsafe(node)]

    assert found == []


def test_no_behaviour_outside_configuration_depends_on_environment_or_debug() -> None:
    # Authentication, RBAC, tenant resolution and data access behave identically in every environment. A branch on
    # ENVIRONMENT or DEBUG elsewhere could hide a bypass that the suite, which runs with ENVIRONMENT=test, never
    # executes. Only app/config.py (production secret requirements) and log messages may read them.
    found = []
    for module, tree in _modules():
        if module == "app/config.py":
            continue
        logged = {id(node) for call in ast.walk(tree)
                  if isinstance(call, ast.Call) and (_called_name(call) or "").startswith("logger.")
                  for node in ast.walk(call)}
        found += [(module, node.lineno) for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and node.attr in {"environment", "debug"} and id(node) not in logged]

    assert found == []


def test_only_the_login_lookup_reads_password_hashes_and_only_registration_writes_one() -> None:
    readers = set()
    for module, tree in _modules():
        docstrings = _docstrings(tree)
        if any(isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
               and "password_hash" in node.value for node in ast.walk(tree)):
            readers.add(module)

    # Tenant reads, tokens and API responses never see a hash (TenantUser and AuthenticatedUser have no such field).
    # Registration (D-035) writes the first admin's hash and returns columns that never include it (checked in
    # test_every_statement_on_a_tenant_owned_table_is_scoped).
    assert readers == {"app/auth.py", "app/registration.py"}
    # Within app/auth.py only the sign-in lookup selects the hash: the demo session's lookup (D-036) never does.
    auth_sql = [sql for module, sql in _tenant_statements() if module == "app/auth.py"]
    assert len(auth_sql) == 2 and sum("password_hash" in sql for sql in auth_sql) == 1


# --- Phase 2.5 -------------------------------------------------------------------------------------------------


def test_only_the_api_database_dependency_owns_transactions() -> None:
    # TenantDatabase never commits (D-020); the request transaction is opened and ended in one place, the API's
    # tenant_database dependency (D-026). A commit, rollback or transaction anywhere else would split it.
    found = {(module, node.func.attr) for module, tree in _modules() for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr in {"commit", "rollback", "transaction"}}

    assert found == {("app/api/database.py", "transaction")}


# --- Phase 5 -------------------------------------------------------------------------------------------------


_SIMILARITY = re.compile(r"<->|<=>|<#>|<\+>|\bl2_distance\b|\bcosine_distance\b|\binner_product\b", re.IGNORECASE)
_JOINED = re.compile(r"\bJOIN\s+(\w+)\s+(\w+)\b", re.IGNORECASE)


def _tenant_joined(sql: str) -> bool:
    """Every joined tenant-owned table (by alias) is itself filtered on the tenant in the WHERE clause."""
    where = re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.IGNORECASE)[-1]
    return all(f"{alias}.company_id = %(company_id)s" in where
               for table, alias in _JOINED.findall(sql) if table in TENANT_OWNED_TABLES)


def test_vector_similarity_search_runs_only_in_tenant_scoped_statements() -> None:
    # Phase 6 (replacing the Phase 5 "no similarity search yet" check): pgvector distance operators appear only in
    # the tenant layer, in statements that are tenant-scoped, keep every joined table in the tenant, and search only
    # one model's embeddings.
    found = [(module, node.value) for module, tree in _modules() for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str) and _SIMILARITY.search(node.value)]

    assert found, "the scan must find the vector search"
    for module, sql in found:
        assert module == "app/tenant_db.py", module
        assert _tenant_scoped(sql) and _tenant_joined(sql), sql
        assert "embedding_model = %(embedding_model)s" in sql, sql


def test_every_joined_tenant_table_must_be_tenant_filtered() -> None:
    # Positive control for _tenant_joined: a join whose table is not filtered on the tenant is refused.
    safe = ("SELECT ce.chunk_id FROM chunk_embeddings ce JOIN policies p ON p.id = ce.policy_id "
            "WHERE ce.company_id = %(company_id)s AND p.company_id = %(company_id)s")
    unsafe = ("SELECT ce.chunk_id FROM chunk_embeddings ce JOIN policies p ON p.id = ce.policy_id "
              "WHERE ce.company_id = %(company_id)s")

    assert _tenant_joined(safe) and not _tenant_joined(unsafe)
    assert _SIMILARITY.search("ORDER BY embedding <=> %(query)s")
