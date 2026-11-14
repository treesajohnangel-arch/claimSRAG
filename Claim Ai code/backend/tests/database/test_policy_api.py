"""Policy and document API against PostgreSQL (Phase 2.4): the whole chain, nothing mocked.

    HTTP request → current_identity (token) → permission_required → current_tenant → tenant_database
        → TenantDatabase → PostgreSQL

World: companies A and B, each with an admin and a staff user, policy 1 (number POL-001 in both companies, with one
document) and policy 2 (POL-002, no documents).

Most tests give the application the test's own connection (replacing only ``database_connection``), so the real
``tenant_database`` transaction runs as a savepoint and everything is rolled back afterwards. The last section uses a
throwaway database and the real connection dependency to show what is committed and what is not.
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from typing import Annotated, Any

import psycopg
import pytest
from fastapi import APIRouter, Depends, HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.authorization import permission_required
from app.api.database import DATABASE_UNAVAILABLE, TenantDb, database_connection
from app.api.policies import (DOCUMENT_NOT_FOUND, DOCUMENT_VERSION_TAKEN, POLICY_HAS_DOCUMENTS, POLICY_NOT_FOUND,
                              POLICY_NUMBER_TAKEN)
from app.auth import authenticate
from app.config import Settings
from app.errors import INTERNAL_ERROR_DETAIL
from app.main import create_app
from app.models import InsuranceType
from app.passwords import hash_password
from app.rbac import FORBIDDEN_MESSAGE, Permission
from app.tenant_db import TenantDatabase
from app.tokens import issue_access_token
from tests.conftest import RsaKeyPair
from tests.database.conftest import run_alembic
from tests.database.test_cross_tenant import ATTACKS, MALFORMED_AUTHORIZATION, _token

pytestmark = pytest.mark.database

Row = dict[str, Any]
Insert = Callable[..., Row]
Conn = psycopg.Connection[Any]
OTHER = {"A": "B", "B": "A"}
PASSWORD = "fake-Passw0rd-for-policy-api-tests"
POLICY_FIELDS = {"id", "policy_number", "name", "insurance_type", "status", "created_at", "updated_at"}
DOCUMENT_FIELDS = {"id", "policy_id", "document_key", "version", "original_filename", "content_type", "size_bytes",
                   "checksum_sha256", "status", "created_at", "updated_at"}


def _new_document(**changes: Any) -> dict[str, Any]:
    return {"document_key": "endorsement", "version": 1, "original_filename": "Endorsement 2026.pdf",
            "content_type": "application/pdf", "size_bytes": 52_000, "checksum_sha256": "ab" * 32, **changes}


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def world(insert_company: Insert, insert_user: Insert, insert_policy: Insert, insert_policy_document: Insert,
          password_hash: str) -> dict[str, Row]:
    rows: dict[str, Row] = {}
    for company in ("A", "B"):
        c = company.lower()
        rows[company] = insert_company(name=f"Company {company}", slug=f"api-{c}")
        cid = rows[company]["id"]
        for role, prefix in (("COMPANY_ADMIN", "admin"), ("STAFF", "staff")):
            rows[f"{prefix}_{c}"] = insert_user(company_id=cid, email=f"{prefix}@{c}.example", role=role,
                                                full_name=f"{prefix} {company}", password_hash=password_hash)
        rows[f"{company}1"] = insert_policy(company_id=cid, policy_number="POL-001", name=f"{company} Health",
                                            insurance_type="HEALTH")
        rows[f"{company}2"] = insert_policy(company_id=cid, policy_number="POL-002", name=f"{company} Motor",
                                            insurance_type="MOTOR")
        rows[f"{company}1-doc"] = insert_policy_document(policy_id=rows[f"{company}1"]["id"], version=1,
                                                         original_filename=f"{company} wording.pdf")
    return rows


@pytest.fixture(scope="module")
def token_settings(jwt_keys: RsaKeyPair) -> Settings:
    return Settings(_env_file=None, jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)


@pytest.fixture
def reached(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every database connection a request opened and every query TenantDatabase ran."""
    events: list[str] = []
    real_fetch = TenantDatabase._fetch

    def recording_fetch(self: TenantDatabase, query: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        events.append("query")
        return real_fetch(self, query, params)

    monkeypatch.setattr(TenantDatabase, "_fetch", recording_fetch)
    return events


@pytest.fixture
def api(token_settings: Settings, db: Conn, reached: list[str]) -> Iterator[TestClient]:
    app = create_app(token_settings)

    def test_connection() -> Iterator[Conn]:
        reached.append("connection")
        yield db

    app.dependency_overrides[database_connection] = test_connection
    with TestClient(app) as client:
        yield client


def _as(world: dict[str, Row], settings: Settings, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(world[who], settings)}"}


def _policy_row(db: Conn, policy_id: Any) -> Row | None:
    return db.execute("SELECT * FROM policies WHERE id = %s", (policy_id,)).fetchone()


def _document_row(db: Conn, document_id: Any) -> Row | None:
    return db.execute("SELECT * FROM policy_documents WHERE id = %s", (document_id,)).fetchone()


def _counts(db: Conn, world: dict[str, Row]) -> tuple[int, ...]:
    query = ("SELECT (SELECT count(*) FROM policies WHERE company_id = %(c)s) AS p, "
             "(SELECT count(*) FROM policy_documents WHERE company_id = %(c)s) AS d")
    return tuple(value for company in ("A", "B")
                 for value in db.execute(query, {"c": world[company]["id"]}).fetchone().values())


# --- Policies -------------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_a_tenant_lists_exactly_its_own_policies_without_company_ids(
    api: TestClient, world: dict[str, Row], token_settings: Settings, company: str
) -> None:
    response = api.get("/policies", headers=_as(world, token_settings, f"admin_{company.lower()}"))

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == [str(world[f"{company}{n}"]["id"]) for n in (1, 2)]
    assert all(set(item) == POLICY_FIELDS for item in body)
    assert str(world[company]["id"]) not in response.text and str(world[OTHER[company]]["id"]) not in response.text


def test_admin_creates_reads_updates_and_deletes_a_policy(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    admin = _as(world, token_settings, "admin_a")

    created = api.post("/policies", json={"policy_number": "TRV-1", "name": "Travel", "insurance_type": "TRAVEL"},
                       headers=admin)
    policy_id = created.json()["id"]
    fetched = api.get(f"/policies/{policy_id}", headers=admin)
    updated = api.patch(f"/policies/{policy_id}", json={"name": "Travel Plus", "insurance_type": "HOME"},
                        headers=admin)
    deleted = api.delete(f"/policies/{policy_id}", headers=admin)

    assert created.status_code == 201
    assert set(created.json()) == POLICY_FIELDS
    assert (created.json()["status"], created.json()["insurance_type"]) == ("ACTIVE", "TRAVEL")
    assert fetched.status_code == 200 and fetched.json() == created.json()
    assert updated.status_code == 200
    assert (updated.json()["name"], updated.json()["insurance_type"], updated.json()["policy_number"]) == (
        "Travel Plus", "HOME", "TRV-1")
    assert deleted.status_code == 204 and deleted.content == b""
    assert api.get(f"/policies/{policy_id}", headers=admin).status_code == 404
    assert _policy_row(db, policy_id) is None


def test_a_created_policy_belongs_to_the_tokens_company(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    response = api.post("/policies", json={"policy_number": "HOME-1", "name": "Home", "insurance_type": "HOME",
                                           "status": "INACTIVE"}, headers=_as(world, token_settings, "admin_b"))

    stored = _policy_row(db, response.json()["id"])
    assert response.status_code == 201 and response.json()["status"] == "INACTIVE"
    assert stored["company_id"] == world["B"]["id"]


def test_a_duplicate_policy_number_is_a_safe_conflict_within_the_tenant_only(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    admin_a = _as(world, token_settings, "admin_a")

    created = api.post("/policies", json={"policy_number": "POL-001", "name": "Copy", "insurance_type": "HOME"},
                       headers=admin_a)
    renumbered = api.patch(f"/policies/{world['A2']['id']}", json={"policy_number": "POL-001"}, headers=admin_a)
    elsewhere = api.post("/policies", json={"policy_number": "POL-003", "name": "New", "insurance_type": "HOME"},
                         headers=_as(world, token_settings, "admin_b"))

    assert (created.status_code, created.json()) == (409, {"detail": POLICY_NUMBER_TAKEN})
    assert (renumbered.status_code, renumbered.json()) == (409, {"detail": POLICY_NUMBER_TAKEN})
    assert elsewhere.status_code == 201  # numbers are per company: B is not told what A uses
    assert _policy_row(db, world["A2"]["id"]) == world["A2"]


@pytest.mark.parametrize(
    ("method", "target", "body", "field"),
    [("POST", None, {"policy_number": " POL-9", "name": "N", "insurance_type": "HOME"}, "policy_number"),
     ("POST", None, {"policy_number": "POL\n9", "name": "N", "insurance_type": "HOME"}, "policy_number"),
     ("POST", None, {"policy_number": "POL-9", "name": "   ", "insurance_type": "HOME"}, "name"),
     ("POST", None, {"policy_number": "POL-9", "name": "x" * 301, "insurance_type": "HOME"}, "name"),
     ("POST", None, {"policy_number": "POL\u00009", "name": "N", "insurance_type": "HOME"}, None),
     ("PATCH", "A2", {"policy_number": ""}, "policy_number")],
    ids=["number-padded", "number-newline", "name-blank", "name-too-long", "number-nul", "patch-number-empty"],
)
def test_values_the_database_refuses_are_a_safe_422(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, method: str, target: str | None,
    body: dict[str, Any], field: str | None,
) -> None:
    path = "/policies" if target is None else f"/policies/{world[target]['id']}"
    before = _counts(db, world)

    response = api.request(method, path, json=body, headers=_as(world, token_settings, "admin_a"))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == (["body", field] if field else ["body"])
    for internal in ("policies", "_check", "constraint", "SQLSTATE", "violates", "NUL"):
        assert internal.lower() not in response.text.lower()
    assert _counts(db, world) == before
    assert _policy_row(db, world["A2"]["id"]) == world["A2"]


def test_unknown_policies_are_404(api: TestClient, world: dict[str, Row], token_settings: Settings) -> None:
    admin, missing = _as(world, token_settings, "admin_a"), uuid.uuid4()

    responses = [api.get(f"/policies/{missing}", headers=admin),
                 api.patch(f"/policies/{missing}", json={"name": "Ghost"}, headers=admin),
                 api.delete(f"/policies/{missing}", headers=admin),
                 api.get(f"/policies/{missing}/documents", headers=admin),
                 api.post(f"/policies/{missing}/documents", json=_new_document(), headers=admin)]

    assert [(r.status_code, r.json()) for r in responses] == [(404, {"detail": POLICY_NOT_FOUND})] * 5


def test_a_policy_with_documents_is_a_conflict_to_delete(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    response = api.delete(f"/policies/{world['A1']['id']}", headers=_as(world, token_settings, "admin_a"))

    assert (response.status_code, response.json()) == (409, {"detail": POLICY_HAS_DOCUMENTS})
    assert _policy_row(db, world["A1"]["id"]) == world["A1"]
    assert _document_row(db, world["A1-doc"]["id"]) == world["A1-doc"]


# --- Staff ----------------------------------------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_staff_reads_policies_and_documents(
    api: TestClient, world: dict[str, Row], token_settings: Settings, company: str
) -> None:
    staff = _as(world, token_settings, f"staff_{company.lower()}")
    policy, document = world[f"{company}1"], world[f"{company}1-doc"]

    responses = [api.get("/policies", headers=staff), api.get(f"/policies/{policy['id']}", headers=staff),
                 api.get(f"/policies/{policy['id']}/documents", headers=staff),
                 api.get(f"/documents/{document['id']}", headers=staff)]

    assert [r.status_code for r in responses] == [200, 200, 200, 200]
    assert len(responses[0].json()) == 2 and responses[1].json()["id"] == str(policy["id"])
    assert [d["id"] for d in responses[2].json()] == [str(document["id"])] == [responses[3].json()["id"]]


@pytest.mark.security
@pytest.mark.parametrize("staff", ["staff_a", "staff_b"])
def test_staff_cannot_manage_and_the_database_is_never_reached(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, reached: list[str], staff: str
) -> None:
    own = staff[-1].upper()
    policy, document = world[f"{own}2"]["id"], world[f"{own}1-doc"]["id"]
    headers = _as(world, token_settings, staff) | {"X-Role": "COMPANY_ADMIN"}
    before = _counts(db, world)

    responses = [api.post("/policies", json={"policy_number": "S-1", "name": "S", "insurance_type": "HOME"},
                          headers=headers),
                 api.patch(f"/policies/{policy}", json={"name": "Staff"}, headers=headers),
                 api.delete(f"/policies/{policy}", headers=headers),
                 api.post(f"/policies/{policy}/documents", json=_new_document(), headers=headers),
                 api.patch(f"/documents/{document}", json={"original_filename": "staff.pdf"}, headers=headers),
                 api.put(f"/documents/{document}/status", json={"status": "INACTIVE"}, headers=headers),
                 api.delete(f"/documents/{document}", headers=headers)]

    assert {(r.status_code, r.text) for r in responses} == {(403, f'{{"detail":"{FORBIDDEN_MESSAGE}"}}')}
    assert reached == []  # no connection opened, no query run
    assert _counts(db, world) == before
    assert _policy_row(db, policy) == world[f"{own}2"] and _document_row(db, document) == world[f"{own}1-doc"]


# --- Cross-tenant ---------------------------------------------------------------------------------------------


def _policy_requests(policy_id: Any) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [("GET", f"/policies/{policy_id}", None), ("PATCH", f"/policies/{policy_id}", {"name": "hijacked"}),
            ("DELETE", f"/policies/{policy_id}", None), ("GET", f"/policies/{policy_id}/documents", None),
            ("POST", f"/policies/{policy_id}/documents", _new_document(document_key="planted"))]


def _document_requests(document_id: Any) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [("GET", f"/documents/{document_id}", None),
            ("PATCH", f"/documents/{document_id}", {"original_filename": "hijacked.pdf"}),
            ("PUT", f"/documents/{document_id}/status", {"status": "INACTIVE"}),
            ("DELETE", f"/documents/{document_id}", None)]


def _outcomes(api: TestClient, headers: dict[str, str], requests: list[Any]) -> list[tuple[int, str]]:
    return [(r.status_code, r.text) for r in (api.request(m, p, json=b, headers=headers) for m, p, b in requests)]


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_another_tenants_policy_is_indistinguishable_from_a_missing_one(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, attacker: str
) -> None:
    headers = _as(world, token_settings, f"admin_{attacker.lower()}")
    victim = OTHER[attacker]
    before = _counts(db, world)

    for name in (f"{victim}1", f"{victim}2"):  # with and without documents
        foreign = _outcomes(api, headers, _policy_requests(world[name]["id"]))
        missing = _outcomes(api, headers, _policy_requests(uuid.uuid4()))
        assert foreign == missing == [(404, f'{{"detail":"{POLICY_NOT_FOUND}"}}')] * 5, name
        assert _policy_row(db, world[name]["id"]) == world[name]
    assert _counts(db, world) == before


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_another_tenants_document_is_indistinguishable_from_a_missing_one(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, attacker: str
) -> None:
    headers = _as(world, token_settings, f"admin_{attacker.lower()}")
    victim = world[f"{OTHER[attacker]}1-doc"]

    foreign = _outcomes(api, headers, _document_requests(victim["id"]))
    missing = _outcomes(api, headers, _document_requests(uuid.uuid4()))

    assert foreign == missing == [(404, f'{{"detail":"{DOCUMENT_NOT_FOUND}"}}')] * 4
    assert _document_row(db, victim["id"]) == victim


# --- Documents ------------------------------------------------------------------------------------------------


def test_admin_records_lists_reads_renames_restatuses_and_deletes_a_document(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    admin, policy_id = _as(world, token_settings, "admin_a"), world["A2"]["id"]

    empty = api.get(f"/policies/{policy_id}/documents", headers=admin)
    created = api.post(f"/policies/{policy_id}/documents", json=_new_document(), headers=admin)
    document_id = created.json()["id"]
    listed = api.get(f"/policies/{policy_id}/documents", headers=admin)
    fetched = api.get(f"/documents/{document_id}", headers=admin)
    renamed = api.patch(f"/documents/{document_id}", json={"original_filename": "Endorsement (signed).pdf"},
                        headers=admin)
    inactive = api.put(f"/documents/{document_id}/status", json={"status": "INACTIVE"}, headers=admin)
    stored = _document_row(db, document_id)
    deleted = api.delete(f"/documents/{document_id}", headers=admin)

    assert (empty.status_code, empty.json()) == (200, [])
    assert created.status_code == 201 and set(created.json()) == DOCUMENT_FIELDS
    assert {k: created.json()[k] for k in _new_document()} == _new_document()
    assert (created.json()["policy_id"], created.json()["status"]) == (str(policy_id), "UPLOADED")
    assert listed.json() == [created.json()] and fetched.json() == created.json()
    assert renamed.status_code == 200 and renamed.json()["original_filename"] == "Endorsement (signed).pdf"
    assert inactive.status_code == 200 and inactive.json()["status"] == "INACTIVE"
    assert (stored["company_id"], stored["policy_id"]) == (world["A"]["id"], policy_id)
    assert (stored["original_filename"], stored["status"]) == ("Endorsement (signed).pdf", "INACTIVE")
    assert deleted.status_code == 204 and deleted.content == b""
    assert api.get(f"/documents/{document_id}", headers=admin).status_code == 404
    assert _policy_row(db, policy_id) == world["A2"]  # deleting the document leaves its policy


@pytest.mark.security
def test_the_storage_reference_is_generated_by_the_server_and_never_exposed(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    admin, policy_id = _as(world, token_settings, "admin_a"), world["A2"]["id"]
    chosen = world["B1-doc"]["storage_reference"]  # another tenant's reference, as a probe
    before = _counts(db, world)

    refused = api.post(f"/policies/{policy_id}/documents", json=_new_document(storage_reference=chosen),
                       headers=admin)
    first = api.post(f"/policies/{policy_id}/documents", json=_new_document(version=1), headers=admin)
    second = api.post(f"/policies/{policy_id}/documents", json=_new_document(version=2), headers=admin)

    assert refused.status_code == 422 and refused.json()["detail"][0]["type"] == "extra_forbidden"
    assert _counts(db, world)[0:2] == (before[0], before[1] + 2)
    references = [_document_row(db, r.json()["id"])["storage_reference"] for r in (first, second)]
    assert len(set(references)) == 2 and chosen not in references
    assert all(len(ref) == 43 and ref.replace("-", "").replace("_", "").isalnum() for ref in references)
    for response in (first, second):
        assert "storage_reference" not in response.json()
        assert not any(ref in response.text for ref in references)


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_a_document_cannot_be_created_under_another_tenants_policy(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, attacker: str
) -> None:
    headers = _as(world, token_settings, f"admin_{attacker.lower()}")
    victim = world[f"{OTHER[attacker]}2"]["id"]
    before = _counts(db, world)

    foreign = api.post(f"/policies/{victim}/documents", json=_new_document(), headers=headers)
    missing = api.post(f"/policies/{uuid.uuid4()}/documents", json=_new_document(), headers=headers)

    assert (foreign.status_code, foreign.text) == (missing.status_code, missing.text)
    assert foreign.json() == {"detail": POLICY_NOT_FOUND}
    assert _counts(db, world) == before


def test_a_duplicate_document_version_is_a_safe_conflict(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    response = api.post(f"/policies/{world['A1']['id']}/documents",
                        json=_new_document(document_key="policy-wording", version=1),
                        headers=_as(world, token_settings, "admin_a"))

    assert (response.status_code, response.json()) == (409, {"detail": DOCUMENT_VERSION_TAKEN})
    assert _counts(db, world)[1] == 1


@pytest.mark.parametrize(
    ("change", "field"),
    [({"document_key": "Policy-Wording"}, "document_key"), ({"document_key": "a b"}, "document_key"),
     ({"original_filename": "../../etc/passwd"}, "original_filename"),
     ({"original_filename": "C:\\docs\\x.pdf"}, "original_filename"),
     ({"original_filename": "x\u0000.pdf"}, None), ({"checksum_sha256": "AB" * 32}, "checksum_sha256"),
     ({"checksum_sha256": "abc"}, "checksum_sha256")],
    ids=["key-upper-case", "key-space", "filename-unix-path", "filename-windows-path", "filename-nul",
         "checksum-upper-case", "checksum-short"],
)
def test_invalid_document_metadata_is_a_safe_422(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, change: dict[str, Any],
    field: str | None,
) -> None:
    before = _counts(db, world)

    response = api.post(f"/policies/{world['A2']['id']}/documents", json=_new_document(**change),
                        headers=_as(world, token_settings, "admin_a"))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == (["body", field] if field else ["body"])
    for internal in ("policy_documents", "_check", "constraint", "violates"):
        assert internal not in response.text.lower()
    assert _counts(db, world) == before


@pytest.mark.parametrize(
    "body",
    [{"original_filename": "x.pdf", "document_key": "endorsement"}, {"original_filename": "x.pdf", "version": 9},
     {"original_filename": "x.pdf", "checksum_sha256": "0" * 64}, {"original_filename": "x.pdf", "size_bytes": 1},
     {"original_filename": "x.pdf", "content_type": "application/pdf"}, {"original_filename": "x.pdf", "status": "INACTIVE"},
     {"original_filename": "x.pdf", "storage_reference": "elsewhere"}, {"original_filename": "x.pdf", "id": str(uuid.uuid4())}],
    ids=["key", "version", "checksum", "size", "content-type", "status", "storage-reference", "id"],
)
def test_document_patch_changes_only_the_file_name(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, body: dict[str, Any]
) -> None:
    document = world["A1-doc"]

    response = api.patch(f"/documents/{document['id']}", json=body, headers=_as(world, token_settings, "admin_a"))

    assert response.status_code == 422  # D-025: a recorded version's identity and file never change in place
    assert _document_row(db, document["id"]) == document


@pytest.mark.security
def test_a_document_cannot_be_moved_to_another_tenants_policy(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    document = world["A1-doc"]
    body = {"original_filename": "moved.pdf", "policy_id": str(world["B1"]["id"]),
            "company_id": str(world["B"]["id"])}

    response = api.patch(f"/documents/{document['id']}", json=body, headers=_as(world, token_settings, "admin_a"))

    assert response.status_code == 422
    assert {item["loc"][-1] for item in response.json()["detail"]} == {"policy_id", "company_id"}
    assert _document_row(db, document["id"]) == document


# --- Untrusted client values and credentials ------------------------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_client_company_role_and_permission_values_never_change_the_tenant(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, company: str
) -> None:
    other = str(world[OTHER[company]]["id"])
    headers = _as(world, token_settings, f"admin_{company.lower()}") | {
        "X-Company-Id": other, "X-Tenant-Id": other, "X-Role": "SUPER_ADMIN", "X-Permission": "policy:manage",
        "Cookie": f"company_id={other}; role=SUPER_ADMIN"}
    params = {"company_id": other, "tenant_id": other, "role": "SUPER_ADMIN", "permission": "policy:manage"}
    before = _counts(db, world)

    listed = api.get("/policies", headers=headers, params=params)
    created = api.post("/policies", headers=headers, params=params,
                       json={"policy_number": "X-1", "name": "X", "insurance_type": "HOME", "company_id": other})

    assert {item["id"] for item in listed.json()} == {str(world[f"{company}{n}"]["id"]) for n in (1, 2)}
    assert created.status_code == 422 and created.json()["detail"][0]["loc"] == ["body", "company_id"]
    assert _counts(db, world) == before


@pytest.mark.security
@pytest.mark.parametrize("attack", list(ATTACKS) + list(MALFORMED_AUTHORIZATION))
def test_forged_expired_foreign_or_malformed_credentials_are_401_before_the_database(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, jwt_keys: RsaKeyPair,
    reached: list[str], attack: str,
) -> None:
    if attack in ATTACKS:
        headers = {"Authorization": f"Bearer {ATTACKS[attack](world, token_settings, jwt_keys)}"}
    else:
        header = MALFORMED_AUTHORIZATION[attack]
        headers = {"Authorization": header} if header is not None else {}
    before = _counts(db, world)

    read = api.get(f"/policies/{world['B1']['id']}", headers=headers)
    write = api.post("/policies", json={"policy_number": "X-1", "name": "X", "insurance_type": "HOME"},
                     headers=headers)

    assert (read.status_code, write.status_code) == (401, 401)
    assert reached == []
    assert _counts(db, world) == before
    for name in ("A Health", "B Health", "POL-001"):
        assert name not in read.text + write.text


@pytest.mark.security
@pytest.mark.parametrize("company", ["A", "B"])
def test_the_real_login_chain_reaches_only_the_users_company(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, company: str
) -> None:
    # credentials → authenticate() against the database → issued token → HTTP → RBAC → tenant → TenantDatabase
    user = authenticate(db, f"api-{company.lower()}", f"admin@{company.lower()}.example", PASSWORD)
    headers = {"Authorization": f"Bearer {issue_access_token(user, token_settings)}"}

    listed = api.get("/policies", headers=headers)
    foreign = api.get(f"/policies/{world[OTHER[company] + '1']['id']}", headers=headers)

    assert [p["name"] for p in listed.json()] == [f"{company} Health", f"{company} Motor"]
    assert foreign.status_code == 404


# --- Transactions with the real connection dependency ----------------------------------------------------------


@pytest.fixture
def live(empty_database: Settings, jwt_keys: RsaKeyPair, connect: Callable[[Settings], Conn]) -> Iterator[Row]:
    """A throwaway database migrated to head, with company A and its admin committed, and the app's real
    database_connection (nothing overridden)."""
    run_alembic(empty_database, "upgrade", "head")
    settings = Settings(_env_file=None, postgres_host=empty_database.postgres_host,
                        postgres_port=empty_database.postgres_port, postgres_user=empty_database.postgres_user,
                        postgres_password=empty_database.postgres_password, postgres_db=empty_database.postgres_db,
                        jwt_private_key=jwt_keys.private_pem, jwt_public_key=jwt_keys.public_pem)
    with connect(empty_database) as conn:
        company = conn.execute("INSERT INTO companies (name, slug) VALUES ('Live A', 'live-a') RETURNING *").fetchone()
        admin = conn.execute(
            "INSERT INTO users (company_id, email, password_hash, full_name, role) "
            "VALUES (%s, 'admin@live.example', %s, 'Admin', 'COMPANY_ADMIN') RETURNING *",
            (company["id"], "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$ZmFrZS1oYXNoLWZvci10ZXN0cw"),
        ).fetchone()
    yield {"settings": settings, "database": empty_database, "admin": admin}


def _committed(connect: Callable[[Settings], Conn], live: Row) -> tuple[int, int]:
    with connect(live["database"]) as conn:  # a separate connection sees only committed data
        row = conn.execute("SELECT (SELECT count(*) FROM policies) AS p, "
                           "(SELECT count(*) FROM policy_documents) AS d").fetchone()
    return row["p"], row["d"]


def test_successful_writes_are_committed_before_the_response_and_refused_ones_leave_nothing(
    live: Row, connect: Callable[[Settings], Conn]
) -> None:
    headers = {"Authorization": f"Bearer {_token(live['admin'], live['settings'])}"}

    with TestClient(create_app(live["settings"])) as client:
        created = client.post("/policies", json={"policy_number": "L-1", "name": "Live", "insurance_type": "HOME"},
                              headers=headers)
        after_create = _committed(connect, live)
        duplicate = client.post("/policies", json={"policy_number": "L-1", "name": "Again", "insurance_type": "HOME"},
                                headers=headers)
        invalid = client.post(f"/policies/{created.json()['id']}/documents",
                              json=_new_document(checksum_sha256="NOT-HEX"), headers=headers)
        document = client.post(f"/policies/{created.json()['id']}/documents", json=_new_document(), headers=headers)
        after_document = _committed(connect, live)
        blocked = client.delete(f"/policies/{created.json()['id']}", headers=headers)
        client.delete(f"/documents/{document.json()['id']}", headers=headers)
        removed = client.delete(f"/policies/{created.json()['id']}", headers=headers)

    assert (created.status_code, after_create) == (201, (1, 0))
    assert (duplicate.status_code, invalid.status_code, blocked.status_code) == (409, 422, 409)
    assert (document.status_code, after_document) == (201, (1, 1))
    assert (removed.status_code, _committed(connect, live)) == (204, (0, 0))


def test_a_request_that_fails_after_writing_commits_nothing(live: Row, connect: Callable[[Settings], Conn]) -> None:
    # Positive control for the transaction boundary: test-only routes that write and then fail.
    app = create_app(live["settings"])
    router = APIRouter(dependencies=[Depends(permission_required(Permission.POLICY_MANAGE))])

    @router.post("/_write_then_refuse")
    def write_then_refuse(db: TenantDb) -> None:
        db.create_policy(policy_number="T-1", name="Temp", insurance_type=InsuranceType.HOME)
        raise HTTPException(409, detail="refused after writing")

    @router.post("/_write_then_crash")
    def write_then_crash(db: TenantDb) -> None:
        db.create_policy(policy_number="T-2", name="Temp", insurance_type=InsuranceType.HOME)
        raise RuntimeError("crash after writing")

    app.include_router(router)
    headers = {"Authorization": f"Bearer {_token(live['admin'], live['settings'])}"}
    with TestClient(app, raise_server_exceptions=False) as client:
        refused = client.post("/_write_then_refuse", headers=headers)
        crashed = client.post("/_write_then_crash", headers=headers)

    assert (refused.status_code, crashed.status_code) == (409, 500)
    assert crashed.json() == {"detail": INTERNAL_ERROR_DETAIL}
    assert _committed(connect, live) == (0, 0)


@pytest.mark.security
def test_an_unavailable_database_is_a_generic_503_without_connection_details(
    live: Row, reached: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    # Since D-035 an unreachable database is a 503 (temporarily unavailable), not an unexplained 500. The response
    # still carries no connection detail, and the server log, which names the reason, never the password.
    settings = live["settings"].model_copy(update={"postgres_db": "claimsrag_test_does_not_exist",
                                                   "postgres_password": SecretStr("wrong-secret-x9")})
    headers = {"Authorization": f"Bearer {_token(live['admin'], settings)}"}

    with caplog.at_level(logging.ERROR, logger="app"), \
            TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get("/policies", headers=headers)

    assert (response.status_code, response.json()) == (503, {"detail": DATABASE_UNAVAILABLE})
    for detail in ("claimsrag_test_does_not_exist", "wrong-secret-x9", settings.postgres_host, "5432", "FATAL"):
        assert detail not in response.text
    assert "database connection failed error=OperationalError" in caplog.text
    assert "wrong-secret-x9" not in caplog.text


# --- Phase 2.5 hardening: injection on document routes, provenance through the status route ---------------------


@pytest.mark.security
@pytest.mark.parametrize("attacker", ["A", "B"])
def test_client_tenant_values_never_unlock_another_tenants_documents(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, attacker: str
) -> None:
    victim = OTHER[attacker]
    claimed = str(world[victim]["id"])
    injected = {"X-Company-Id": claimed, "X-Tenant-Id": claimed, "X-Role": "COMPANY_ADMIN",
                "X-Permission": "policy:manage", "Cookie": f"company_id={claimed}; tenant_id={claimed}"}
    params = {"company_id": claimed, "tenant_id": claimed, "role": "COMPANY_ADMIN", "permission": "policy:manage"}
    headers = _as(world, token_settings, f"admin_{attacker.lower()}")
    requests = (_document_requests(world[f"{victim}1-doc"]["id"]) + _policy_requests(world[f"{victim}1"]["id"]))
    before = _counts(db, world)

    plain = _outcomes(api, headers, requests)
    claiming = [(r.status_code, r.text) for r in (api.request(m, p, json=b, headers=headers | injected, params=params)
                                                  for m, p, b in requests)]

    assert claiming == plain and {status for status, _ in plain} == {404}
    assert _document_row(db, world[f"{victim}1-doc"]["id"]) == world[f"{victim}1-doc"]
    assert _counts(db, world) == before


@pytest.mark.security
@pytest.mark.parametrize("field", ["original_filename", "document_key", "version", "policy_id", "company_id",
                                   "storage_reference", "content_type", "size_bytes", "checksum_sha256"])
def test_the_status_route_changes_only_the_status(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings, field: str
) -> None:
    document = world["A1-doc"]
    smuggled = {"original_filename": "x.pdf", "document_key": "endorsement", "version": 9,
                "policy_id": str(world["B1"]["id"]), "company_id": str(world["B"]["id"]),
                "storage_reference": world["B1-doc"]["storage_reference"], "content_type": "application/pdf",
                "size_bytes": 1, "checksum_sha256": "0" * 64}[field]

    response = api.put(f"/documents/{document['id']}/status", json={"status": "INACTIVE", field: smuggled},
                       headers=_as(world, token_settings, "admin_a"))

    assert response.status_code == 422 and response.json()["detail"][0]["type"] == "extra_forbidden"
    assert _document_row(db, document["id"]) == document


# --- Phase 5: a document with embeddings ------------------------------------------------------------------------


@pytest.mark.security
def test_deleting_a_document_with_embeddings_is_a_safe_conflict(
    api: TestClient, db: Conn, world: dict[str, Row], token_settings: Settings
) -> None:
    from app.api.policies import DOCUMENT_HAS_EMBEDDINGS
    from app.tenancy import TenantIdentity

    document = world["A1-doc"]
    TenantDatabase(db, TenantIdentity(company_id=world["A"]["id"])).insert_chunk_embedding(
        policy_id=document["policy_id"], document_id=document["id"], document_version=document["version"],
        chunk_id=uuid.uuid4(), chunk_index=0, page_start=1, page_end=1, section_heading=None,
        chunk_text="Embedded wording.", embedding_model="test/fake-embedder", embedding=[0.5] * 384)

    response = api.delete(f"/documents/{document['id']}", headers=_as(world, token_settings, "admin_a"))

    assert (response.status_code, response.json()) == (409, {"detail": DOCUMENT_HAS_EMBEDDINGS})
    assert "chunk_embeddings" not in response.text and "constraint" not in response.text
    assert _document_row(db, document["id"]) == document
