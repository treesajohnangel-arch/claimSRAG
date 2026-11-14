"""Tenant-scoped reads of policies and policy documents against the real schema (Phase 2.1).

Company A has policies A-HEALTH (two versions of its policy wording, plus an endorsement) and A-MOTOR (one document).
Company B has B-HEALTH, which deliberately reuses A-HEALTH's policy number and document key. Each test runs in a
transaction that is rolled back, in the throwaway test database.
"""

import uuid
from collections.abc import Callable
from typing import Any

import psycopg
import pytest

from app.models import DocumentStatus, InsuranceType, Policy, PolicyDocument, PolicyStatus, UserRole
from app.tenancy import TenantIdentity, resolve_tenant
from app.tenant_db import TenantDatabase
from app.tokens import TokenIdentity

pytestmark = [pytest.mark.database, pytest.mark.security]

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
OTHER = {"A": "B", "B": "A"}
POLICIES = ("A-HEALTH", "A-MOTOR", "B-HEALTH")
DOCUMENTS = ("A-HEALTH-v1", "A-HEALTH-v2", "A-HEALTH-end", "A-MOTOR-v1", "B-HEALTH-v1")


@pytest.fixture
def catalog(insert_company: Insert, insert_policy: Insert, insert_policy_document: Insert) -> dict[str, Row]:
    a = insert_company(name="Company A", slug="policy-tenant-a")
    b = insert_company(name="Company B", slug="policy-tenant-b")
    rows = {"A": a, "B": b}
    rows["A-HEALTH"] = insert_policy(company_id=a["id"], policy_number="POL-001", insurance_type="HEALTH")
    rows["A-MOTOR"] = insert_policy(company_id=a["id"], policy_number="POL-002", insurance_type="MOTOR")
    rows["B-HEALTH"] = insert_policy(company_id=b["id"], policy_number="POL-001", insurance_type="HEALTH")
    for name, policy, key, version in (("A-HEALTH-v1", "A-HEALTH", "policy-wording", 1),
                                       ("A-HEALTH-v2", "A-HEALTH", "policy-wording", 2),
                                       ("A-HEALTH-end", "A-HEALTH", "endorsement", 1),
                                       ("A-MOTOR-v1", "A-MOTOR", "policy-wording", 1),
                                       ("B-HEALTH-v1", "B-HEALTH", "policy-wording", 1)):
        rows[name] = insert_policy_document(policy_id=rows[policy]["id"], document_key=key, version=version)
    return rows


def _scoped(db: Conn, company: Row) -> TenantDatabase:
    return TenantDatabase(db, TenantIdentity(company_id=company["id"]))


def _ids(records: list[Any]) -> set[uuid.UUID]:
    return {record.id for record in records}


@pytest.mark.parametrize(("company", "own"), [("A", {"A-HEALTH", "A-MOTOR"}), ("B", {"B-HEALTH"})])
def test_each_tenant_lists_exactly_its_own_policies(
    db: Conn, catalog: dict[str, Row], company: str, own: set[str]
) -> None:
    policies = _scoped(db, catalog[company]).list_policies()

    assert _ids(policies) == {catalog[name]["id"] for name in own}
    assert all(isinstance(p, Policy) and p.company_id == catalog[company]["id"] for p in policies)
    assert [p.policy_number for p in policies] == sorted(p.policy_number for p in policies)


@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_cannot_get_another_tenants_policy_by_id(db: Conn, catalog: dict[str, Row], company: str) -> None:
    tenant = _scoped(db, catalog[company])
    own, foreign = f"{company}-HEALTH", f"{OTHER[company]}-HEALTH"

    found = tenant.get_policy(catalog[own]["id"])

    assert found is not None and found.id == catalog[own]["id"]
    assert (found.insurance_type, found.status) == (InsuranceType.HEALTH, PolicyStatus.ACTIVE)
    assert tenant.get_policy(catalog[foreign]["id"]) is None  # same policy number, other company: nothing
    assert tenant.get_policy(uuid.uuid4()) is None  # indistinguishable from an id that does not exist


def test_a_tenant_lists_the_documents_of_its_own_policy_in_version_order(db: Conn, catalog: dict[str, Row]) -> None:
    documents = _scoped(db, catalog["A"]).list_documents(catalog["A-HEALTH"]["id"])

    assert [(d.document_key, d.version) for d in documents] == [
        ("endorsement", 1), ("policy-wording", 1), ("policy-wording", 2)]
    assert _ids(documents) == {catalog[n]["id"] for n in ("A-HEALTH-v1", "A-HEALTH-v2", "A-HEALTH-end")}
    assert all(isinstance(d, PolicyDocument) and d.status is DocumentStatus.UPLOADED for d in documents)
    # Another policy of the same tenant keeps its own documents.
    assert _ids(_scoped(db, catalog["A"]).list_documents(catalog["A-MOTOR"]["id"])) == {catalog["A-MOTOR-v1"]["id"]}


@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_cannot_list_another_tenants_documents(db: Conn, catalog: dict[str, Row], company: str) -> None:
    tenant = _scoped(db, catalog[company])
    for name in (name for name in POLICIES if not name.startswith(company)):
        assert tenant.list_documents(catalog[name]["id"]) == [], name
    assert tenant.list_documents(uuid.uuid4()) == []


@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_cannot_get_another_tenants_document_by_id(db: Conn, catalog: dict[str, Row], company: str) -> None:
    tenant = _scoped(db, catalog[company])
    own = catalog[f"{company}-HEALTH-v1"]

    found = tenant.get_document(own["id"])

    assert found is not None and (found.id, found.policy_id) == (own["id"], own["policy_id"])
    assert found.checksum_sha256 == own["checksum_sha256"] and found.storage_reference == own["storage_reference"]
    for name in (name for name in DOCUMENTS if not name.startswith(company)):
        assert tenant.get_document(catalog[name]["id"]) is None, name
    assert tenant.get_document(uuid.uuid4()) is None


def test_a_company_id_cannot_be_supplied_to_widen_the_tenant(db: Conn, catalog: dict[str, Row]) -> None:
    tenant_a = _scoped(db, catalog["A"])
    b = catalog["B"]["id"]

    with pytest.raises(TypeError):
        tenant_a.list_policies(company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.get_policy(catalog["B-HEALTH"]["id"], company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.list_documents(catalog["B-HEALTH"]["id"], company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.get_document(catalog["B-HEALTH-v1"]["id"], company_id=b)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        tenant_a.get_policy(str(catalog["B-HEALTH"]["id"]))  # type: ignore[arg-type]  # ids are UUIDs, never text

    assert _ids(tenant_a.list_policies()) == {catalog["A-HEALTH"]["id"], catalog["A-MOTOR"]["id"]}


@pytest.mark.parametrize("company", ["A", "B"])
def test_the_verified_identitys_tenant_is_the_only_authority(db: Conn, catalog: dict[str, Row], company: str) -> None:
    # TokenIdentity (as decode_access_token builds it) → resolve_tenant → TenantDatabase: the only route to the data.
    identity = TokenIdentity(user_id=uuid.uuid4(), company_id=catalog[company]["id"], role=UserRole.COMPANY_ADMIN)
    tenant = TenantDatabase(db, resolve_tenant(identity))

    assert {p.company_id for p in tenant.list_policies()} == {catalog[company]["id"]}
    assert tenant.get_document(catalog[f"{OTHER[company]}-HEALTH-v1"]["id"]) is None


def test_tenants_without_policies_get_nothing_not_a_fallback(
    db: Conn, catalog: dict[str, Row], insert_company: Insert
) -> None:
    assert _scoped(db, insert_company()).list_policies() == []
    assert _scoped(db, {"id": uuid.uuid4()}).list_policies() == []  # a tenant id with no company at all


def test_a_cross_tenant_document_cannot_exist_to_be_read(db: Conn, catalog: dict[str, Row],
                                                         insert_policy_document: Insert) -> None:
    # The relationship is refused by the database (test_policy_documents.py), so no tenant can ever be handed a
    # document of its own company that hangs off another company's policy, or the reverse.
    with pytest.raises(psycopg.errors.ForeignKeyViolation), db.transaction():
        insert_policy_document(company_id=catalog["A"]["id"], policy_id=catalog["B-HEALTH"]["id"], version=9)

    assert _scoped(db, catalog["A"]).list_documents(catalog["B-HEALTH"]["id"]) == []
    assert _ids(_scoped(db, catalog["B"]).list_documents(catalog["B-HEALTH"]["id"])) == {catalog["B-HEALTH-v1"]["id"]}
