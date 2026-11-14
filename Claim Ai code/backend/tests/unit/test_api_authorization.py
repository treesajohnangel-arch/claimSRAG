"""Unit tests for the API authorization dependency, called directly (no application, no HTTP, no database).

``permission_required(p)`` returns the dependency ``check``. FastAPI gives it the identity from ``current_identity``;
here it is called with a TokenIdentity directly. Expected results are derived from the real ROLE_PERMISSIONS.
"""

import ast
import uuid
from pathlib import Path
from types import MappingProxyType
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException
from fastapi.dependencies.utils import get_dependant

import app.api.authorization as authorization
import app.rbac as rbac
from app.api.authorization import NOT_AUTHENTICATED_MESSAGE, current_identity, permission_required
from app.models import UserRole
from app.rbac import FORBIDDEN_MESSAGE, ROLE_PERMISSIONS, Permission
from app.tokens import TokenIdentity

CASES = [(role, permission, permission in ROLE_PERMISSIONS[role]) for role in UserRole for permission in Permission]
CASE_IDS = [f"{role.value}-{permission.value}" for role, permission, _ in CASES]


def _identity(role: Any = UserRole.STAFF) -> TokenIdentity:
    if isinstance(role, UserRole):
        return TokenIdentity(user_id=uuid.uuid4(), company_id=uuid.uuid4(), role=role)
    return TokenIdentity.model_construct(user_id=uuid.uuid4(), company_id=uuid.uuid4(), role=role)


def _forbidden(permission: Permission, identity: Any) -> HTTPException:
    with pytest.raises(HTTPException) as excinfo:
        permission_required(permission)(identity)
    assert excinfo.value.status_code == 403
    return excinfo.value


# --- Allow and deny, derived from the real mapping ------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(("role", "permission", "allowed"), CASES, ids=CASE_IDS)
def test_dependency_follows_the_role_permission_mapping(role: UserRole, permission: Permission, allowed: bool) -> None:
    identity = _identity(role)

    if allowed:
        assert permission_required(permission)(identity) is identity  # passed through unchanged
    else:
        error = _forbidden(permission, identity)
        assert error.detail == FORBIDDEN_MESSAGE
        assert not error.headers  # 403 is not an authentication challenge


@pytest.mark.security
@pytest.mark.parametrize("role", ["UNKNOWN", "SUPER_ADMIN", "COMPANY_ADMIN", None],
                         ids=["unknown", "super-admin", "string-not-member", "missing"])
def test_unknown_role_is_denied_every_permission(role: Any) -> None:
    for permission in Permission:
        _forbidden(permission, _identity(role))


@pytest.mark.security
def test_role_missing_from_the_mapping_is_denied_every_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType({UserRole.COMPANY_ADMIN: frozenset(Permission)}))

    for permission in Permission:
        _forbidden(permission, _identity(UserRole.STAFF))


@pytest.mark.security
def test_permission_granted_to_no_role_is_denied_to_everyone(monkeypatch: pytest.MonkeyPatch) -> None:
    reduced = {role: granted - {Permission.POLICY_MANAGE} for role, granted in ROLE_PERMISSIONS.items()}
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType(reduced))

    for role in UserRole:
        _forbidden(Permission.POLICY_MANAGE, _identity(role))


@pytest.mark.security
@pytest.mark.parametrize("permission", ["policy:delete", "policy:read", "", None, "*"],
                         ids=["unknown", "string-not-member", "empty", "none", "wildcard"])
def test_unknown_permission_cannot_even_be_declared(permission: Any) -> None:
    with pytest.raises(TypeError, match="Permission member"):
        permission_required(permission)


# --- Authentication stays in current_identity -----------------------------------------------------------------


@pytest.mark.security
def test_missing_identity_is_an_authentication_failure() -> None:
    with pytest.raises(HTTPException) as excinfo:
        current_identity(None, None)  # type: ignore[arg-type]  # no Authorization credentials

    assert excinfo.value.status_code == 401
    assert excinfo.value.detail == NOT_AUTHENTICATED_MESSAGE
    assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}


@pytest.mark.security
def test_check_gets_its_identity_only_from_current_identity_and_reads_no_client_input() -> None:
    dependant = get_dependant(path="/", call=permission_required(Permission.POLICY_READ))

    # One input: the verified identity from current_identity. No query, path, header, cookie or body parameter
    # through which a client could supply a role or a company id.
    assert [dependency.call for dependency in dependant.dependencies] == [current_identity]
    for client_params in (dependant.query_params, dependant.path_params, dependant.header_params,
                          dependant.cookie_params, dependant.body_params):
        assert client_params == []


@pytest.mark.security
def test_check_accepts_no_role_or_company_argument() -> None:
    check = permission_required(Permission.POLICY_MANAGE)

    with pytest.raises(TypeError):
        check(_identity(UserRole.STAFF), role=UserRole.COMPANY_ADMIN)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        check(_identity(UserRole.STAFF), company_id=uuid.uuid4())  # type: ignore[call-arg]


@pytest.mark.security
def test_decision_uses_the_identitys_role_only() -> None:
    user_id, company_id = uuid.uuid4(), uuid.uuid4()
    staff = TokenIdentity(user_id=user_id, company_id=company_id, role=UserRole.STAFF)
    admin = TokenIdentity(user_id=user_id, company_id=company_id, role=UserRole.COMPANY_ADMIN)

    # Same user and company: only the verified role changes the outcome.
    assert permission_required(Permission.POLICY_MANAGE)(admin) is admin
    _forbidden(Permission.POLICY_MANAGE, staff)


@pytest.mark.security
def test_check_does_not_decode_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_decoding(*args: object) -> None:
        raise AssertionError("the permission check must not decode a token")

    monkeypatch.setattr(authorization, "decode_access_token", no_decoding)
    admin = _identity(UserRole.COMPANY_ADMIN)

    assert permission_required(Permission.COMPANY_READ)(admin) is admin
    _forbidden(Permission.COMPANY_READ, _identity(UserRole.STAFF))


@pytest.mark.security
def test_permission_decisions_never_touch_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[object] = []
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: attempts.append(args))

    for role, permission, allowed in CASES:
        if allowed:
            permission_required(permission)(_identity(role))
        else:
            _forbidden(permission, _identity(role))

    assert attempts == []
    for module in (rbac, authorization):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        imported = {getattr(node, "module", None) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        assert not {"psycopg", "app.db", "app.tenant_db", "app.auth"} & imported


def test_each_check_records_its_declared_permission() -> None:
    for permission in Permission:
        assert permission_required(permission).required_permission is permission  # type: ignore[attr-defined]
