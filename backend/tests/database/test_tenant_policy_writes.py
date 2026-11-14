"""Tenant-scoped policy writes against the real schema (Phase 2.2).

Company A owns policy A1 (number POL-001, with no documents) and A2 (POL-002, with two document versions). Company B
owns B1, which deliberately reuses the number POL-001. Each test runs in a transaction that is rolled back, in the
throwaway test database. A statement expected to fail runs inside ``db.transaction()`` (a savepoint).
"""

import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest
from psycopg import errors
from pydantic import ValidationError

from app.models import InsuranceType, Policy, PolicyStatus, UserRole
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase, TenantScopeError
from app.tokens import TokenIdentity

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
OTHER = {"A": "B", "B": "A"}


@pytest.fixture
def tenants(insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert) -> dict[str, Row]:
    a = insert_company(name="Company A", slug="writes-tenant-a")
    b = insert_company(name="Company B", slug="writes-tenant-b")
    rows = {"A": a, "B": b,
            "A1": insert_policy(company_id=a["id"], policy_number="POL-001", name="A Health", insurance_type="HEALTH"),
            "A2": insert_policy(company_id=a["id"], policy_number="POL-002", name="A Motor", insurance_type="MOTOR"),
            "B1": insert_policy(company_id=b["id"], policy_number="POL-001", name="B Health", insurance_type="HEALTH")}
    for version in (1, 2):
        insert_policy_document(policy_id=rows["A2"]["id"], version=version)
    return rows


def _scoped(db: Conn, company: Row) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company["id"]))


def _row(db: Conn, policy_id: uuid.UUID) -> Row | None:
    return db.execute("SELECT * FROM policies WHERE id = %s", (policy_id,)).fetchone()


def _count(db: Conn, table: str, company: Row) -> int:
    query = {"policies": "SELECT count(*) AS n FROM policies WHERE company_id = %s",
             "policy_documents": "SELECT count(*) AS n FROM policy_documents WHERE company_id = %s"}[table]
    return db.execute(query, (company["id"],)).fetchone()["n"]


# --- Create ---------------------------------------------------------------------------------------------------


def test_create_policy_stores_the_tenants_company_a_generated_id_and_the_default_status(
    db: Conn, tenants: dict[str, Row]
) -> None:
    created = _scoped(db, tenants["A"]).create_policy(policy_number="TRV-2026-01", name="Annual Travel",
                                                      insurance_type=InsuranceType.TRAVEL)

    stored = _row(db, created.id)
    assert isinstance(created, Policy) and created.id.version == 4
    assert created.company_id == tenants["A"]["id"] == stored["company_id"]
    assert (created.policy_number, created.name) == ("TRV-2026-01", "Annual Travel")
    assert (created.insurance_type, created.status) == (InsuranceType.TRAVEL, PolicyStatus.ACTIVE)
    assert created.updated_at == created.created_at
    assert _scoped(db, tenants["A"]).get_policy(created.id) == created


@pytest.mark.parametrize("insurance_type", list(InsuranceType))
def test_every_insurance_type_can_be_created(db: Conn, tenants: dict[str, Row], insurance_type: InsuranceType) -> None:
    created = _scoped(db, tenants["A"]).create_policy(policy_number=f"N-{insurance_type}", name="Cover",
                                                      insurance_type=insurance_type, status=PolicyStatus.INACTIVE)

    assert (created.insurance_type, created.status) == (insurance_type, PolicyStatus.INACTIVE)


@pytest.mark.parametrize(("field", "value"), [("insurance_type", "LIFE"), ("insurance_type", "HEALTH"),
                                              ("status", "DRAFT"), ("status", "ACTIVE")],
                         ids=["unknown-type", "type-as-text", "unknown-status", "status-as-text"])
def test_values_outside_the_vocabularies_are_rejected_before_the_database(
    db: Conn, tenants: dict[str, Row], field: str, value: str
) -> None:
    values = {"policy_number": "NEW-1", "name": "Cover", "insurance_type": InsuranceType.HOME, field: value}

    with pytest.raises(TypeError):
        _scoped(db, tenants["A"]).create_policy(**values)
    assert _count(db, "policies", tenants["A"]) == 2


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [("policy_number", "", "policies_policy_number_check"), ("policy_number", "   ", "policies_policy_number_check"),
     ("policy_number", " POL-9", "policies_policy_number_check"), ("policy_number", "POL\n9",
                                                                   "policies_policy_number_check"),
     ("policy_number", "x" * 101, "policies_policy_number_check"), ("name", "", "policies_name_check"),
     ("name", "   ", "policies_name_check"), ("name", "x" * 301, "policies_name_check")],
    ids=["number-empty", "number-blank", "number-padded", "number-newline", "number-too-long", "name-empty",
         "name-blank", "name-too-long"],
)
def test_invalid_policy_number_or_name_is_rejected_by_the_database(
    db: Conn, tenants: dict[str, Row], field: str, value: str, constraint: str
) -> None:
    values = {"policy_number": "NEW-1", "name": "Cover", "insurance_type": InsuranceType.HOME, field: value}

    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        _scoped(db, tenants["A"]).create_policy(**values)

    assert excinfo.value.diag.constraint_name == constraint
    assert _count(db, "policies", tenants["A"]) == 2


def test_a_duplicate_policy_number_in_the_same_tenant_is_rejected(db: Conn, tenants: dict[str, Row]) -> None:
    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        _scoped(db, tenants["A"]).create_policy(policy_number="POL-001", name="Copy", insurance_type=InsuranceType.HOME)

    assert excinfo.value.diag.constraint_name == "policies_company_id_policy_number_key"


def test_the_same_policy_number_in_another_tenant_succeeds(db: Conn, tenants: dict[str, Row]) -> None:
    created = _scoped(db, tenants["B"]).create_policy(policy_number="POL-002", name="B Motor",
                                                      insurance_type=InsuranceType.MOTOR)

    assert created.company_id == tenants["B"]["id"]
    assert created.policy_number == tenants["A2"]["policy_number"]


# --- Update and status ----------------------------------------------------------------------------------------


def test_update_changes_only_the_given_fields_and_advances_updated_at(db: Conn, tenants: dict[str, Row]) -> None:
    before = tenants["A1"]

    renamed = _scoped(db, tenants["A"]).update_policy(before["id"], name="A Health Plus")
    changed = _scoped(db, tenants["A"]).update_policy(before["id"], policy_number="POL-001-R",
                                                      insurance_type=InsuranceType.TRAVEL)

    assert renamed is not None and changed is not None
    assert (renamed.name, renamed.policy_number, renamed.insurance_type) == ("A Health Plus", "POL-001",
                                                                            InsuranceType.HEALTH)
    assert (changed.name, changed.policy_number, changed.insurance_type) == ("A Health Plus", "POL-001-R",
                                                                            InsuranceType.TRAVEL)
    assert (changed.id, changed.company_id, changed.status) == (before["id"], before["company_id"], PolicyStatus.ACTIVE)
    assert changed.created_at == before["created_at"]
    assert before["updated_at"] < renamed.updated_at < changed.updated_at


def test_update_keeps_the_constraints(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    with pytest.raises(errors.UniqueViolation) as excinfo, db.transaction():
        tenant_a.update_policy(tenants["A1"]["id"], policy_number="POL-002")
    assert excinfo.value.diag.constraint_name == "policies_company_id_policy_number_key"
    with pytest.raises(errors.CheckViolation) as excinfo, db.transaction():
        tenant_a.update_policy(tenants["A1"]["id"], policy_number=" POL-001")
    assert excinfo.value.diag.constraint_name == "policies_policy_number_check"
    with pytest.raises(TypeError):
        tenant_a.update_policy(tenants["A1"]["id"], insurance_type="LIFE")  # type: ignore[arg-type]
    assert _row(db, tenants["A1"]["id"]) == tenants["A1"]


def test_a_policy_with_documents_can_be_updated_and_keeps_them(db: Conn, tenants: dict[str, Row]) -> None:
    # The composite (company_id, policy_id) link is untouched by an update: the documents stay attached.
    tenant_a = _scoped(db, tenants["A"])

    tenant_a.update_policy(tenants["A2"]["id"], policy_number="POL-002-R")

    assert [d.version for d in tenant_a.list_documents(tenants["A2"]["id"])] == [1, 2]


def test_status_changes_between_active_and_inactive(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    inactive = tenant_a.set_policy_status(tenants["A1"]["id"], PolicyStatus.INACTIVE)
    active = tenant_a.set_policy_status(tenants["A1"]["id"], PolicyStatus.ACTIVE)

    assert inactive is not None and active is not None
    assert (inactive.status, active.status) == (PolicyStatus.INACTIVE, PolicyStatus.ACTIVE)
    assert (active.name, active.policy_number) == (tenants["A1"]["name"], tenants["A1"]["policy_number"])
    assert tenants["A1"]["updated_at"] < inactive.updated_at < active.updated_at
    with pytest.raises(TypeError):
        tenant_a.set_policy_status(tenants["A1"]["id"], "ARCHIVED")  # type: ignore[arg-type]


def test_updating_a_missing_policy_changes_nothing(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    assert tenant_a.update_policy(uuid.uuid4(), name="Ghost") is None
    assert tenant_a.set_policy_status(uuid.uuid4(), PolicyStatus.INACTIVE) is None


# --- Delete ---------------------------------------------------------------------------------------------------


def test_a_policy_without_documents_can_be_deleted(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a = _scoped(db, tenants["A"])

    assert tenant_a.delete_policy(tenants["A1"]["id"]) is True

    assert _row(db, tenants["A1"]["id"]) is None
    assert tenant_a.get_policy(tenants["A1"]["id"]) is None
    assert tenant_a.delete_policy(tenants["A1"]["id"]) is False  # already gone


def test_deleting_a_missing_policy_returns_false(db: Conn, tenants: dict[str, Row]) -> None:
    assert _scoped(db, tenants["A"]).delete_policy(uuid.uuid4()) is False
    assert _count(db, "policies", tenants["A"]) == 2


def test_a_policy_with_documents_is_never_deleted_and_nothing_cascades(db: Conn, tenants: dict[str, Row]) -> None:
    with pytest.raises(errors.RestrictViolation) as excinfo, db.transaction():
        _scoped(db, tenants["A"]).delete_policy(tenants["A2"]["id"])

    assert excinfo.value.diag.constraint_name == "policy_documents_policy_fkey"
    assert _row(db, tenants["A2"]["id"]) == tenants["A2"]
    assert _count(db, "policy_documents", tenants["A"]) == 2
    assert [d.version for d in _scoped(db, tenants["A"]).list_documents(tenants["A2"]["id"])] == [1, 2]


# --- Security: the write boundary -----------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_another_tenants_policy_cannot_be_updated_re_statused_or_deleted(
    db: Conn, tenants: dict[str, Row], company: str
) -> None:
    attacker = _scoped(db, tenants[company])
    victims = [name for name in ("A1", "A2", "B1") if not name.startswith(company)]

    for name in victims:
        policy_id = tenants[name]["id"]
        assert attacker.update_policy(policy_id, name="hijacked", policy_number="HIJACK") is None
        assert attacker.set_policy_status(policy_id, PolicyStatus.INACTIVE) is None
        assert attacker.delete_policy(policy_id) is False
        assert _row(db, policy_id) == tenants[name]  # every column, updated_at included, untouched


@pytest.mark.security
def test_a_guessed_foreign_id_is_indistinguishable_from_a_missing_one(db: Conn, tenants: dict[str, Row]) -> None:
    attacker = _scoped(db, tenants["A"])

    def outcomes(policy_id: uuid.UUID) -> tuple[Any, ...]:
        return (attacker.get_policy(policy_id), attacker.update_policy(policy_id, name="x"),
                attacker.set_policy_status(policy_id, PolicyStatus.ACTIVE), attacker.delete_policy(policy_id))

    assert outcomes(tenants["B1"]["id"]) == outcomes(uuid.uuid4()) == (None, None, None, False)


@pytest.mark.security
def test_a_caller_cannot_supply_a_company_id_or_move_a_policy(db: Conn, tenants: dict[str, Row]) -> None:
    tenant_a, b = _scoped(db, tenants["A"]), tenants["B"]["id"]

    with pytest.raises(TypeError):
        tenant_a.create_policy(policy_number="X-1", name="X", insurance_type=InsuranceType.HOME,
                               company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.update_policy(tenants["A1"]["id"], company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.update_policy(tenants["A1"]["id"], id=tenants["B1"]["id"])  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.set_policy_status(tenants["A1"]["id"], PolicyStatus.ACTIVE, company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.delete_policy(tenants["B1"]["id"], company_id=b)  # type: ignore[call-arg]

    updated = tenant_a.update_policy(tenants["A1"]["id"], name="Still A")
    assert updated is not None and (updated.id, updated.company_id) == (tenants["A1"]["id"], tenants["A"]["id"])
    assert (_count(db, "policies", tenants["A"]), _count(db, "policies", tenants["B"])) == (2, 1)


@pytest.mark.security
def test_an_altered_or_invalid_tenant_identity_cannot_write(db: Conn, tenants: dict[str, Row]) -> None:
    b = tenants["B"]["id"]
    for forged in (SimpleNamespace(company_id=b), {"company_id": b}, TenantIdentity.model_construct(company_id=str(b)),
                   TenantIdentity.model_construct(company_id=uuid.UUID(int=0)), None):
        with pytest.raises(TenantScopeError):
            TenantDatabase(db, forged)  # type: ignore[arg-type]

    tenant = TenantIdentity(company_id=tenants["A"]["id"])
    tenant_a = TenantDatabase(db, tenant)
    with pytest.raises(ValidationError):
        tenant.company_id = b  # type: ignore[misc]  # frozen
    with pytest.raises(AttributeError):
        tenant_a._tenant = TenantIdentity(company_id=b)  # type: ignore[misc]  # immutable

    created = tenant_a.create_policy(policy_number="X-1", name="X", insurance_type=InsuranceType.HOME)
    assert created.company_id == tenants["A"]["id"]
    assert _count(db, "policies", tenants["B"]) == 1


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_writes_follow_the_verified_identitys_tenant(db: Conn, tenants: dict[str, Row], company: str) -> None:
    # TokenIdentity (as decode_access_token builds it) → resolve_tenant → TenantDatabase: the only tenant source.
    identity = TokenIdentity(user_id=uuid.uuid4(), company_id=tenants[company]["id"], role=UserRole.COMPANY_ADMIN)
    tenant_db = TenantDatabase(db, resolve_tenant(identity))

    created = tenant_db.create_policy(policy_number="TOKEN-1", name="From token", insurance_type=InsuranceType.HOME)

    assert created.company_id == tenants[company]["id"]
    assert tenant_db.delete_policy(tenants[f"{OTHER[company]}1"]["id"]) is False
