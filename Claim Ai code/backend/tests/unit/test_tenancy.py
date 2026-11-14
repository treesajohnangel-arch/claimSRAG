"""Unit tests for tenant resolution (no FastAPI, no database, no network).

The trusted tenant is the company id of a verified TokenIdentity. Anything else fails closed.
"""

import inspect
import uuid
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from pydantic import ValidationError

from app.auth import AuthenticatedUser
from app.config import Settings
from app.logging_config import configure_logging
from app.models import UserRole, UserStatus
from app.tenancy import TENANT_RESOLUTION_FAILURE_MESSAGE, TenantIdentity, TenantResolutionError, resolve_tenant
from app.tokens import TokenIdentity

COMPANY_A, COMPANY_B = uuid.uuid4(), uuid.uuid4()
NIL = uuid.UUID(int=0)


def _identity(company_id: uuid.UUID = COMPANY_A, role: UserRole = UserRole.STAFF) -> TokenIdentity:
    return TokenIdentity(user_id=uuid.uuid4(), company_id=company_id, role=role)


def _unresolvable(identity: object) -> TenantResolutionError:
    with pytest.raises(TenantResolutionError) as excinfo:
        resolve_tenant(identity)  # type: ignore[arg-type]
    return excinfo.value


# --- Resolution -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(UserRole))
def test_verified_identity_resolves_to_its_own_company(role: UserRole) -> None:
    tenant = resolve_tenant(_identity(COMPANY_A, role))

    assert isinstance(tenant, TenantIdentity)
    assert tenant == TenantIdentity(company_id=COMPANY_A)


@pytest.mark.security
def test_users_of_different_companies_resolve_different_tenants_and_colleagues_the_same() -> None:
    alice_a, bob_a, carol_b = _identity(COMPANY_A), _identity(COMPANY_A, UserRole.COMPANY_ADMIN), _identity(COMPANY_B)

    assert resolve_tenant(alice_a) == resolve_tenant(bob_a) == TenantIdentity(company_id=COMPANY_A)
    assert resolve_tenant(carol_b) == TenantIdentity(company_id=COMPANY_B)
    assert resolve_tenant(alice_a) != resolve_tenant(carol_b)


def test_resolution_does_not_depend_on_the_role() -> None:
    # RBAC is separate (app/rbac.py): a role grants permissions, it does not change the tenant.
    assert {resolve_tenant(_identity(COMPANY_A, role)) for role in UserRole} == {TenantIdentity(company_id=COMPANY_A)}


# --- Fail closed ----------------------------------------------------------------------------------------------


@pytest.mark.security
def test_identity_without_a_company_id_fails_closed() -> None:
    _unresolvable(TokenIdentity.model_construct(user_id=uuid.uuid4(), role=UserRole.STAFF))


@pytest.mark.security
@pytest.mark.parametrize(
    "company_id",
    ["not-a-uuid", "", None, 123, str(COMPANY_A), COMPANY_A.bytes, NIL, str(NIL)],
    ids=["not-a-uuid", "empty", "none", "int", "uuid-string", "uuid-bytes", "nil-uuid", "nil-uuid-string"],
)
def test_malformed_company_id_fails_closed(company_id: Any) -> None:
    # A validated TokenIdentity can only hold a UUID; one built without validation must still not yield a tenant.
    _unresolvable(TokenIdentity.model_construct(user_id=uuid.uuid4(), company_id=company_id, role=UserRole.STAFF))


@pytest.mark.security
@pytest.mark.parametrize(
    "candidate",
    [
        {"user_id": str(uuid.uuid4()), "company_id": str(COMPANY_B), "role": "STAFF"},
        SimpleNamespace(user_id=uuid.uuid4(), company_id=COMPANY_B, role=UserRole.STAFF),
        AuthenticatedUser(user_id=uuid.uuid4(), company_id=COMPANY_B, email="a@acme.example", full_name="A",
                          role=UserRole.STAFF, status=UserStatus.ACTIVE),
        COMPANY_B,
        str(COMPANY_B),
        None,
    ],
    ids=["request-payload", "look-alike-object", "identity-not-from-a-token", "bare-uuid", "uuid-string", "none"],
)
def test_anything_but_a_verified_identity_cannot_produce_a_tenant(candidate: object) -> None:
    _unresolvable(candidate)


@pytest.mark.security
def test_failures_are_generic_and_never_fall_back_to_a_tenant() -> None:
    failures = [
        _unresolvable(None),
        _unresolvable({"company_id": str(COMPANY_A)}),
        _unresolvable(TokenIdentity.model_construct(user_id=uuid.uuid4(), company_id=NIL, role=UserRole.STAFF)),
    ]

    for error in failures:
        assert str(error) == TENANT_RESOLUTION_FAILURE_MESSAGE == "Tenant could not be resolved"
        assert error.args == (TENANT_RESOLUTION_FAILURE_MESSAGE,)
        assert str(COMPANY_A) not in str(error)


# --- The tenant identity ---------------------------------------------------------------------------------------


@pytest.mark.security
def test_tenant_identity_holds_only_the_company_id() -> None:
    tenant = TenantIdentity.model_validate(
        {"company_id": COMPANY_A, "password": "fake-password", "password_hash": "$argon2id$fake", "token": "a.b.c"}
    )

    assert set(TenantIdentity.model_fields) == {"company_id"}
    assert tenant.model_dump() == {"company_id": COMPANY_A}
    for value in ("fake-password", "$argon2id$fake", "a.b.c"):
        assert value not in repr(tenant) and value not in tenant.model_dump_json()


@pytest.mark.security
def test_tenant_identity_cannot_be_changed_or_built_with_the_nil_uuid() -> None:
    tenant = resolve_tenant(_identity(COMPANY_A))

    with pytest.raises(ValidationError):
        tenant.company_id = COMPANY_B  # type: ignore[misc]
    with pytest.raises(ValidationError):
        TenantIdentity(company_id=NIL)

    assert tenant.company_id == COMPANY_A


@pytest.mark.security
def test_resolver_takes_no_company_id_argument() -> None:
    assert list(inspect.signature(resolve_tenant).parameters) == ["identity"]
    with pytest.raises(TypeError):
        resolve_tenant(_identity(COMPANY_A), company_id=COMPANY_B)  # type: ignore[call-arg]


def test_resolution_does_not_touch_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    # The autouse network guard already fails any connection; this also records that none is attempted.
    attempts: list[object] = []
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: attempts.append(args))

    resolve_tenant(_identity(COMPANY_A))
    _unresolvable(None)

    assert attempts == []


@pytest.mark.security
def test_failure_is_logged_without_identity_contents(settings: Settings, capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(settings)
    broken = TokenIdentity.model_construct(user_id=uuid.uuid4(), company_id="client-text", role=UserRole.STAFF)

    resolve_tenant(_identity(COMPANY_A))
    _unresolvable(broken)
    _unresolvable({"company_id": str(COMPANY_B), "note": "client-note"})
    logs = capsys.readouterr().err

    assert f"tenant resolution failed: no valid company_id user_id={broken.user_id}" in logs
    assert "tenant resolution failed: no valid company_id user_id=None" in logs
    assert logs.count("tenant resolution") == 2  # success is not logged
    for value in ("client-text", "client-note", str(COMPANY_A), str(COMPANY_B)):
        assert value not in logs
