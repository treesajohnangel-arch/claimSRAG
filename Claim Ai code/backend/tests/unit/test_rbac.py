"""Unit tests for role-based access control (no database, no HTTP).

RBAC decides whether a role grants a permission. It does not decide tenancy (a later Phase 1 task).
"""

import inspect
import re
import uuid
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import app.rbac as rbac
from app.auth import AuthenticatedUser, AuthenticationError
from app.config import Settings
from app.logging_config import configure_logging
from app.models import UserRole, UserStatus
from app.rbac import (
    FORBIDDEN_MESSAGE,
    ROLE_PERMISSIONS,
    AuthorizationError,
    Permission,
    has_permission,
    permissions_for,
    require_permission,
)
from app.tokens import AccessTokenError, TokenIdentity, decode_access_token, issue_access_token
from tests.conftest import RsaKeyPair, forge_claims

ADMIN, STAFF = UserRole.COMPANY_ADMIN, UserRole.STAFF

# The approved V1 matrix, written out here independently of app/rbac.py: changing a grant there must also change
# this table. Values are (COMPANY_ADMIN, STAFF).
MATRIX = {
    "company:read": (True, False),
    "company:user:read": (True, False),
    "company:user:manage": (True, False),
    "policy:read": (True, True),
    "policy:manage": (True, False),
    "search:use": (True, True),
    "chat:use": (True, True),
}
CASES = [
    (role, permission, allowed)
    for permission, grants in MATRIX.items()
    for role, allowed in zip((ADMIN, STAFF), grants, strict=True)
]
CASE_IDS = [f"{role.value}-{permission}" for role, permission, _ in CASES]


def _identity(role: UserRole = STAFF, company_id: uuid.UUID | None = None) -> TokenIdentity:
    return TokenIdentity(user_id=uuid.uuid4(), company_id=company_id or uuid.uuid4(), role=role)


def _granted(identity: object) -> set[Permission]:
    return {permission for permission in Permission if has_permission(identity, permission)}


def _assert_denied_everything(identity: object) -> None:
    assert _granted(identity) == set()
    for permission in Permission:
        with pytest.raises(AuthorizationError):
            require_permission(identity, permission)  # type: ignore[arg-type]


# --- Vocabulary and mapping -----------------------------------------------------------------------------------


def test_permission_vocabulary_is_exactly_the_v1_matrix() -> None:
    assert {permission.value for permission in Permission} == set(MATRIX)
    # One name per capability: no enum aliases.
    assert len(Permission.__members__) == len(MATRIX)
    for permission in Permission:
        assert re.fullmatch(r"[a-z]+(:[a-z]+){1,2}", permission.value), permission


def test_mapping_covers_exactly_the_existing_roles() -> None:
    assert set(UserRole) == {ADMIN, STAFF}
    assert set(ROLE_PERMISSIONS) == set(UserRole)


@pytest.mark.parametrize("index, role", [(0, ADMIN), (1, STAFF)], ids=["COMPANY_ADMIN", "STAFF"])
def test_mapping_is_exactly_the_approved_matrix(index: int, role: UserRole) -> None:
    expected = {permission for permission, grants in MATRIX.items() if grants[index]}

    assert {permission.value for permission in ROLE_PERMISSIONS[role]} == expected
    assert {permission.value for permission in permissions_for(role)} == expected


@pytest.mark.security
def test_mapping_cannot_be_changed_at_runtime() -> None:
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[STAFF] = frozenset(Permission)  # type: ignore[index]
    with pytest.raises(AttributeError):
        ROLE_PERMISSIONS[STAFF].add(Permission.POLICY_MANAGE)  # type: ignore[attr-defined]

    assert Permission.POLICY_MANAGE not in permissions_for(STAFF)


# --- The permission matrix ------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(("role", "permission", "allowed"), CASES, ids=CASE_IDS)
def test_permission_matrix(role: UserRole, permission: str, allowed: bool) -> None:
    identity = _identity(role)

    assert has_permission(identity, Permission(permission)) is allowed
    if allowed:
        assert require_permission(identity, Permission(permission)) is None
    else:
        with pytest.raises(AuthorizationError) as excinfo:
            require_permission(identity, Permission(permission))
        assert str(excinfo.value) == FORBIDDEN_MESSAGE


# --- Roles ----------------------------------------------------------------------------------------------------


def test_company_admin_and_staff_are_recognised() -> None:
    assert permissions_for(ADMIN) == frozenset(Permission)
    assert permissions_for(STAFF) == {Permission.POLICY_READ, Permission.SEARCH_USE, Permission.CHAT_USE}


@pytest.mark.security
@pytest.mark.parametrize(
    "role",
    ["UNKNOWN", "SUPER_ADMIN", "ADMIN", "", None, "STAFF", "COMPANY_ADMIN", "staff", "company_admin", " STAFF", 1,
     ["STAFF"], {"role": "STAFF"}],
    ids=["unknown", "super-admin", "admin", "empty", "none", "plain-string-staff", "plain-string-admin",
         "lower-case-staff", "lower-case-admin", "padded", "int", "list", "dict"],
)
def test_anything_but_an_existing_role_gets_no_permissions(role: object) -> None:
    # Roles are compared only as UserRole members: no string, case or whitespace variant is accepted.
    assert permissions_for(role) == frozenset()


@pytest.mark.security
@pytest.mark.parametrize("role", ["UNKNOWN", "SUPER_ADMIN", "COMPANY_ADMIN", ""], ids=["unknown", "super-admin",
                                                                                       "string-admin", "empty"])
def test_identity_with_an_unknown_role_is_denied_everything(role: str) -> None:
    # A validated TokenIdentity cannot hold anything but a UserRole member ...
    if role != "COMPANY_ADMIN":
        with pytest.raises(ValidationError):
            TokenIdentity(user_id=uuid.uuid4(), company_id=uuid.uuid4(), role=role)  # type: ignore[arg-type]
    # ... and one built without validation is still denied, never given STAFF or COMPANY_ADMIN permissions.
    _assert_denied_everything(TokenIdentity.model_construct(user_id=uuid.uuid4(), company_id=uuid.uuid4(), role=role))


@pytest.mark.security
def test_identity_without_a_role_is_denied_everything() -> None:
    _assert_denied_everything(TokenIdentity.model_construct(user_id=uuid.uuid4(), company_id=uuid.uuid4()))


def test_role_checks_are_deterministic() -> None:
    by_value, by_member = _identity(UserRole("STAFF")), _identity(STAFF)

    results = {frozenset(_granted(identity)) for identity in (by_value, by_member) for _ in range(5)}

    assert results == {frozenset(permissions_for(STAFF))}


@pytest.mark.security
@pytest.mark.parametrize(
    ("mapping", "role"),
    [
        ({ADMIN: frozenset(Permission)}, STAFF),
        ({STAFF: ROLE_PERMISSIONS[STAFF]}, ADMIN),
        ({ADMIN: frozenset(Permission), STAFF: None}, STAFF),
        ({ADMIN: None, STAFF: ROLE_PERMISSIONS[STAFF]}, ADMIN),
        ({ADMIN: frozenset(Permission), STAFF: ["policy:read"]}, STAFF),
        ({}, STAFF),
        ({}, ADMIN),
    ],
    ids=["staff-missing", "admin-missing", "staff-none", "admin-none", "staff-not-a-frozenset", "empty-staff",
         "empty-admin"],
)
def test_missing_or_malformed_mapping_fails_closed(
    monkeypatch: pytest.MonkeyPatch, mapping: dict[Any, Any], role: UserRole
) -> None:
    monkeypatch.setattr(rbac, "ROLE_PERMISSIONS", MappingProxyType(mapping))

    # No fallback to another role's permissions (an unmapped COMPANY_ADMIN must not get STAFF's), and no default allow.
    _assert_denied_everything(_identity(role))


@pytest.mark.security
def test_grants_come_only_from_the_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    # With a reduced mapping, COMPANY_ADMIN gets exactly what it lists: there is no hidden "admin can do anything"
    # rule and no role check outside the mapping.
    monkeypatch.setattr(
        rbac,
        "ROLE_PERMISSIONS",
        MappingProxyType({ADMIN: frozenset({Permission.POLICY_READ}), STAFF: frozenset({Permission.CHAT_USE})}),
    )

    assert _granted(_identity(ADMIN)) == {Permission.POLICY_READ}
    assert _granted(_identity(STAFF)) == {Permission.CHAT_USE}


# --- Permissions ----------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("role", list(UserRole))
@pytest.mark.parametrize(
    "permission",
    ["policy:delete", "admin:all", "*", "policy:*", "company:user:delete", "POLICY:READ", "", None, "policy:read"],
    ids=["unknown", "admin-all", "wildcard", "resource-wildcard", "unknown-nested", "upper-case", "empty", "none",
         "string-not-member"],
)
def test_unknown_empty_or_non_member_permission_is_denied(role: UserRole, permission: object) -> None:
    # COMPANY_ADMIN is not a wildcard, and a plain string equal to a real permission is not that permission.
    identity = _identity(role)

    assert has_permission(identity, permission) is False
    with pytest.raises(AuthorizationError):
        require_permission(identity, permission)  # type: ignore[arg-type]


# --- Errors ---------------------------------------------------------------------------------------------------


@pytest.mark.security
def test_authorization_error_is_generic_and_distinct_from_authentication_failures() -> None:
    with pytest.raises(AuthorizationError) as excinfo:
        require_permission(_identity(STAFF), Permission.POLICY_MANAGE)
    error = excinfo.value

    assert str(error) == FORBIDDEN_MESSAGE == "Permission denied"
    assert error.args == (FORBIDDEN_MESSAGE,)
    for detail in ("policy", "STAFF", "COMPANY_ADMIN", str(Permission.POLICY_MANAGE)):
        assert detail not in str(error)
    # 403 (authenticated, not allowed) is never confused with 401 (not authenticated).
    for authentication_error in (AccessTokenError, AuthenticationError):
        assert not issubclass(AuthorizationError, authentication_error)
        assert not issubclass(authentication_error, AuthorizationError)


# --- Privilege escalation -------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize(
    "candidate",
    [
        {"user_id": str(uuid.uuid4()), "company_id": str(uuid.uuid4()), "role": "COMPANY_ADMIN",
         "permissions": [permission.value for permission in Permission]},
        SimpleNamespace(user_id=uuid.uuid4(), company_id=uuid.uuid4(), role=ADMIN),
        AuthenticatedUser(user_id=uuid.uuid4(), company_id=uuid.uuid4(), email="a@acme.example", full_name="A",
                          role=ADMIN, status=UserStatus.ACTIVE),
        None,
    ],
    ids=["request-payload", "look-alike-object", "identity-not-from-a-token", "none"],
)
def test_anything_but_a_token_identity_is_denied_everything(candidate: object) -> None:
    _assert_denied_everything(candidate)


@pytest.mark.security
def test_permission_check_takes_no_role_or_company_argument() -> None:
    staff = _identity(STAFF)

    assert list(inspect.signature(require_permission).parameters) == ["identity", "permission"]
    with pytest.raises(TypeError):
        require_permission(staff, Permission.POLICY_MANAGE, role=ADMIN)  # type: ignore[call-arg]


@pytest.mark.security
def test_identity_role_cannot_be_changed_in_place() -> None:
    staff = _identity(STAFF)

    with pytest.raises(ValidationError):
        staff.role = ADMIN  # type: ignore[misc]

    assert staff.role is STAFF
    assert not has_permission(staff, Permission.POLICY_MANAGE)


@pytest.mark.security
def test_tampered_jwt_never_reaches_rbac_as_a_valid_identity(jwt_keys: RsaKeyPair) -> None:
    settings = Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)
    staff_user = AuthenticatedUser(user_id=uuid.uuid4(), company_id=uuid.uuid4(), email="s@acme.example",
                                   full_name="S", role=STAFF, status=UserStatus.ACTIVE)
    token = issue_access_token(staff_user, settings)

    # The forged role fails signature validation, so no identity exists to authorize ...
    with pytest.raises(AccessTokenError):
        decode_access_token(forge_claims(token, role="COMPANY_ADMIN"), settings)
    # ... and the genuine token yields the STAFF identity, which is denied admin permissions.
    identity = decode_access_token(token, settings)
    assert identity.role is STAFF
    assert _granted(identity) == set(permissions_for(STAFF))
    with pytest.raises(AuthorizationError):
        require_permission(identity, Permission.POLICY_MANAGE)


# --- Boundary and logs ----------------------------------------------------------------------------------------


@pytest.mark.security
def test_rbac_alone_is_not_tenant_isolation() -> None:
    # Admins of two different companies hold the same permissions: RBAC never looks at company_id. Whether a given
    # resource belongs to the identity's company is tenant authorization, which is not implemented here.
    admin_of_a, admin_of_b = _identity(ADMIN), _identity(ADMIN)

    assert admin_of_a.company_id != admin_of_b.company_id
    assert _granted(admin_of_a) == _granted(admin_of_b) == set(Permission)


@pytest.mark.security
def test_denials_are_logged_with_ids_only_and_grants_are_not_logged(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    staff = _identity(STAFF)
    payload = {"role": "COMPANY_ADMIN", "note": "client-text-must-not-be-logged"}

    require_permission(staff, Permission.POLICY_READ)
    for identity, permission in ((staff, Permission.POLICY_MANAGE), (payload, Permission.COMPANY_READ),
                                 (staff, "client-supplied-permission")):
        with pytest.raises(AuthorizationError):
            require_permission(identity, permission)  # type: ignore[arg-type]
    logs = capsys.readouterr().err

    assert f"authorization denied user_id={staff.user_id} permission=policy:manage" in logs
    assert "authorization denied user_id=None permission=company:read" in logs
    assert f"authorization denied user_id={staff.user_id} permission=<unknown>" in logs
    assert "permission=policy:read" not in logs  # a granted check is not logged
    for client_text in ("client-text-must-not-be-logged", "client-supplied-permission", "COMPANY_ADMIN"):
        assert client_text not in logs
