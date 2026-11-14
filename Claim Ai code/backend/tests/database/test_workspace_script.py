"""The local operator tooling (scripts/workspace.py, D-032) against PostgreSQL and the real embedding model: a company
and users that can sign in, and a PDF indexed through the application's own services so it is searchable, with page
and section provenance, by its own company only.
"""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.auth import AuthenticationError, authenticate
from app.embeddings import FastEmbedProvider
from app.models import InsuranceType, UserRole
from app.retrieval import retrieve
from app.tenancy import TenantIdentity
from app.tenant_db import TenantDatabase
from scripts.workspace import WorkspaceError, create_company, create_user, index_document, sample_pdf
from tests.database.test_retrieval_quality import provider  # noqa: F401  (fixture)

pytestmark = [pytest.mark.database, pytest.mark.embedding_model]

Conn = psycopg.Connection[Any]
PASSWORD = "operator-test-password-1"  # a fake test credential


@pytest.fixture
def workspace(db: Conn, tmp_path: Path) -> dict[str, Any]:
    companies = {name: create_company(db, name=f"Company {name}", slug=f"ws-{name.lower()}") for name in ("A", "B")}
    for name, row in companies.items():
        TenantDatabase(db, TenantIdentity(company_id=row["id"])).create_policy(
            policy_number="H-100", name=f"Health Plus {name}", insurance_type=InsuranceType.HEALTH)
    pdf = tmp_path / "sample-wording.pdf"
    pdf.write_bytes(sample_pdf())
    return {"companies": companies, "pdf": pdf, "storage": tmp_path / "storage"}


def test_created_users_can_sign_in_with_their_password_only(db: Conn, workspace: dict[str, Any]) -> None:
    created = create_user(db, company_slug="WS-A", email="  Admin@WS-A.example ", full_name="Asha Admin",
                          role=UserRole.COMPANY_ADMIN, password=PASSWORD)

    identity = authenticate(db, "ws-a", "admin@ws-a.example", PASSWORD)

    assert (created["email"], created["role"]) == ("admin@ws-a.example", "COMPANY_ADMIN")
    assert (identity.company_id, identity.role) == (workspace["companies"]["A"]["id"], UserRole.COMPANY_ADMIN)
    with pytest.raises(AuthenticationError):
        authenticate(db, "ws-b", "admin@ws-a.example", PASSWORD)  # another company's code
    stored = db.execute("SELECT password_hash FROM users WHERE id = %s", (created["id"],)).fetchone()
    assert stored is not None and PASSWORD not in str(stored) and str(stored).count("$argon2id$") == 1


@pytest.mark.parametrize(("slug", "password", "message"),
                         [("ws-a", "short", "at least 12"), ("ws-missing", PASSWORD, "No company")],
                         ids=["short-password", "unknown-company"])
def test_user_creation_refuses_short_passwords_and_unknown_companies(
    db: Conn, workspace: dict[str, Any], slug: str, password: str, message: str
) -> None:
    with pytest.raises(WorkspaceError, match=message):
        create_user(db, company_slug=slug, email="x@ws.example", full_name="X", role=UserRole.STAFF,
                    password=password)


@pytest.mark.security
def test_an_indexed_pdf_is_searchable_by_its_own_company_with_page_and_section(
    db: Conn, workspace: dict[str, Any], provider: FastEmbedProvider  # noqa: F811
) -> None:
    counts = index_document(db, company_slug="ws-a", policy_number="H-100", pdf=workspace["pdf"],
                            storage_root=workspace["storage"], embedder=provider)

    tenant_a = TenantDatabase(db, TenantIdentity(company_id=workspace["companies"]["A"]["id"]))
    tenant_b = TenantDatabase(db, TenantIdentity(company_id=workspace["companies"]["B"]["id"]))
    document = tenant_a.get_document(counts["document_id"])
    data = workspace["pdf"].read_bytes()
    assert counts["pages"] == 4 and counts["chunks"] == counts["embedded"] >= 4
    assert document is not None and (document.original_filename, document.version, document.size_bytes,
                                     document.checksum_sha256) == ("sample-wording.pdf", 1, len(data),
                                                                   hashlib.sha256(data).hexdigest())
    assert (workspace["storage"] / document.storage_reference).read_bytes() == data

    hits = retrieve(tenant_a, provider, "waiting period for maternity expenses", top_k=3).hits
    assert hits[0].page_start == 3 and hits[0].section_heading == "SECTION 3 - WAITING PERIODS"
    assert "Maternity expenses" in hits[0].chunk_text
    assert retrieve(tenant_b, provider, "waiting period for maternity expenses", top_k=3).hits == ()
    assert tenant_b.get_document(counts["document_id"]) is None


@pytest.mark.security
def test_indexing_into_another_companys_policy_number_finds_only_its_own_policies(
    db: Conn, workspace: dict[str, Any], provider: FastEmbedProvider, insert_policy: Callable[..., Any]  # noqa: F811
) -> None:
    insert_policy(company_id=workspace["companies"]["A"]["id"], policy_number="ONLY-A")

    with pytest.raises(WorkspaceError, match="no policy numbered"):
        index_document(db, company_slug="ws-b", policy_number="ONLY-A", pdf=workspace["pdf"],
                       storage_root=workspace["storage"], embedder=provider)
