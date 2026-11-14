"""Local operator tooling for a ClaimsRAG workspace (Frontend & Final Integration, D-032). Run from ``backend/``:

    .venv/bin/python -m scripts.workspace create-company --name "Acme Health" --slug acme-health
    .venv/bin/python -m scripts.workspace create-user --company acme-health --email admin@acme.example \\
        --name "Asha Admin" --role COMPANY_ADMIN          # password: prompted, or CLAIMSRAG_USER_PASSWORD
    .venv/bin/python -m scripts.workspace index-document --company acme-health --policy H-100 --pdf wording.pdf
    .venv/bin/python -m scripts.workspace sample-pdf --out sample-policy.pdf

The API has no routes for creating companies or users, or for uploading a file, so these steps run here, on the
server, by someone with database access. This is not part of the served application: ``app/`` never imports it, and
it is never reachable over HTTP.

- **Companies and users** are inserted directly, as the test fixtures do: there is no tenant before the company
  exists, and no user-creation method in ``TenantDatabase``. Passwords are hashed with ``app.passwords`` (Argon2id).
  A password is never taken from the command line (it would be kept in shell history and visible in the process
  list): it is prompted for, or read from ``CLAIMSRAG_USER_PASSWORD``.
- **Indexing a document** is ``app.indexing.index_document``, the same service as the upload route (D-035), in the
  company's ``TenantDatabase``: store, record, ingest, chunk and embed, in one transaction. The policy must already
  exist. Files go to ``DOCUMENT_STORAGE_DIR`` (the application's setting) unless ``--storage-dir`` says otherwise.
- **Companies with their first admin** can also be created in the application (``POST /auth/register-company``);
  this tool remains for operators, and for adding further users (the API has no user management).
- **Never printed:** passwords, hashes, policy text, storage references.
"""

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
import pymupdf
from psycopg.rows import dict_row

from app.config import Settings
from app.db import connect
from app.emails import normalize_email
from app.embeddings import EmbeddingError, FastEmbedProvider
from app.indexing import index_document as index_pdf
from app.ingestion import IngestionError
from app.models import UserRole
from app.passwords import hash_password, password_problem
from app.storage import LocalDocumentStorage
from app.tenancy import TenantIdentity
from app.tenant_db import TenantDatabase

PASSWORD_ENV = "CLAIMSRAG_USER_PASSWORD"


class WorkspaceError(Exception):
    """A refusal with a message that is safe to print."""


def create_company(conn: psycopg.Connection[Any], *, name: str, slug: str) -> dict[str, Any]:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("INSERT INTO companies (name, slug) VALUES (%(name)s, %(slug)s) RETURNING id, name, slug",
                          {"name": name.strip(), "slug": slug.strip().lower()}).fetchone()
    assert row is not None
    return row


def _company_id(conn: psycopg.Connection[Any], slug: str) -> Any:
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("SELECT id FROM companies WHERE slug = %(slug)s", {"slug": slug.strip().lower()}).fetchone()
    if row is None:
        raise WorkspaceError(f"No company with the slug {slug!r}")
    return row["id"]


def create_user(conn: psycopg.Connection[Any], *, company_slug: str, email: str, full_name: str, role: UserRole,
                password: str) -> dict[str, Any]:
    problem = password_problem(password)  # the application's policy for new accounts
    if problem:
        raise WorkspaceError(f"The password is not acceptable: {problem}")
    params = {"company_id": _company_id(conn, company_slug), "email": normalize_email(email),
              "full_name": full_name.strip(), "role": role.value, "password_hash": hash_password(password)}
    with conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute("INSERT INTO users (company_id, email, password_hash, full_name, role) "
                          "VALUES (%(company_id)s, %(email)s, %(password_hash)s, %(full_name)s, %(role)s) "
                          "RETURNING id, email, role", params).fetchone()
    assert row is not None
    return row


def index_document(conn: psycopg.Connection[Any], *, company_slug: str, policy_number: str, pdf: Path,
                   storage_root: Path, embedder: Any, document_key: str = "policy-wording",
                   version: int = 1) -> dict[str, Any]:
    """Store, record, ingest, chunk and embed one PDF as a version of the company's policy. Returns counts only."""
    db = TenantDatabase(conn, TenantIdentity(company_id=_company_id(conn, company_slug)))
    policy = next((p for p in db.list_policies() if p.policy_number == policy_number), None)
    if policy is None:
        raise WorkspaceError(f"The company has no policy numbered {policy_number!r}")
    indexed = index_pdf(db, LocalDocumentStorage(storage_root), embedder, policy_id=policy.id,
                        data=pdf.read_bytes(), original_filename=pdf.name, document_key=document_key, version=version)
    return {"document_id": indexed.document.id, "pages": indexed.page_count, "chunks": indexed.chunk_count,
            "embedded": indexed.embedded}


# A synthetic wording for demonstrations and the end-to-end check. It is not a real policy and names no insurer.
SAMPLE_WORDING: list[list[str]] = [
    ["SAMPLE HEALTH POLICY WORDING",
     "This document is a synthetic sample created for demonstrating ClaimsRAG. It is not issued by any insurer and "
     "does not describe any real insurance product.",
     "SECTION 1 - DEFINITIONS",
     "Hospitalisation means admission to a hospital for a minimum period of 24 consecutive hours for in-patient care.",
     "Pre-existing disease means any condition, ailment or injury that was diagnosed by a physician within 48 "
     "months before the first policy issued by the insurer."],
    ["SECTION 2 - COVERAGE",
     "The insurer will pay the medical expenses for in-patient hospitalisation, including room, boarding and nursing "
     "expenses, up to the sum insured stated in the policy schedule.",
     "Room rent is payable up to 1% of the sum insured per day. Intensive care unit charges are payable up to 2% of "
     "the sum insured per day.",
     "Road ambulance charges are reimbursed up to Rs 2,000 per hospitalisation."],
    ["SECTION 3 - WAITING PERIODS",
     "Expenses related to the treatment of a pre-existing disease are covered only after a waiting period of 36 "
     "months of continuous coverage.",
     "Maternity expenses, including normal and caesarean delivery, are covered after a waiting period of 9 months "
     "from the first policy start date.",
     "Cataract surgery is covered after a waiting period of 24 months, subject to a limit of Rs 40,000 per eye."],
    ["SECTION 4 - EXCLUSIONS",
     "Cosmetic or plastic surgery is not covered unless it is required for reconstruction following an accident.",
     "Dental treatment is excluded unless it requires hospitalisation as a result of an accident.",
     "Expenses arising from self-inflicted injury are not covered."],
]


def sample_pdf() -> bytes:
    with pymupdf.open() as pdf:
        for paragraphs in SAMPLE_WORDING:
            page = pdf.new_page(width=595, height=842)
            y = 72.0
            for text in paragraphs:
                heading = text.isupper()
                rect = pymupdf.Rect(72, y, 523, y + 140)
                spare = page.insert_textbox(rect, text, fontsize=13 if heading else 11,
                                            fontname="hebo" if heading else "helv")
                y += 140 - spare + 18
        return pdf.tobytes()


def _password() -> str:
    password = os.environ.get(PASSWORD_ENV)
    if password is None:
        password = getpass.getpass("New user's password: ")
        if getpass.getpass("Repeat the password: ") != password:
            raise WorkspaceError("The passwords do not match")
    return password


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.workspace", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    company = commands.add_parser("create-company", help="create a company (a tenant)")
    company.add_argument("--name", required=True)
    company.add_argument("--slug", required=True, help="the company code users type when signing in")
    user = commands.add_parser("create-user", help="create a user in a company (password prompted)")
    user.add_argument("--company", required=True, help="the company slug")
    user.add_argument("--email", required=True)
    user.add_argument("--name", required=True)
    user.add_argument("--role", required=True, choices=[role.value for role in UserRole])
    index = commands.add_parser("index-document", help="store a PDF as a policy document and index it for search")
    index.add_argument("--company", required=True, help="the company slug")
    index.add_argument("--policy", required=True, help="the policy number (the policy must already exist)")
    index.add_argument("--pdf", required=True, type=Path)
    index.add_argument("--document-key", default="policy-wording")
    index.add_argument("--version", type=int, default=1)
    index.add_argument("--storage-dir", type=Path, default=None, help="default: the DOCUMENT_STORAGE_DIR setting")
    sample = commands.add_parser("sample-pdf", help="write the synthetic sample policy wording PDF")
    sample.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "sample-pdf":
            args.out.write_bytes(sample_pdf())
            print(f"Wrote the sample policy wording to {args.out}")
            return 0
        password = _password() if args.command == "create-user" else None
        embedder = FastEmbedProvider() if args.command == "index-document" else None
        settings = Settings()
        with connect(settings) as conn, conn.transaction():
            if args.command == "create-company":
                row = create_company(conn, name=args.name, slug=args.slug)
                print(f"Created company {row['slug']}")
            elif args.command == "create-user":
                assert password is not None
                row = create_user(conn, company_slug=args.company, email=args.email, full_name=args.name,
                                  role=UserRole(args.role), password=password)
                print(f"Created {row['role']} user {row['email']}")
            else:
                counts = index_document(conn, company_slug=args.company, policy_number=args.policy, pdf=args.pdf,
                                        storage_root=args.storage_dir or settings.document_storage_dir,
                                        embedder=embedder,
                                        document_key=args.document_key, version=args.version)
                print(f"Indexed document {counts['document_id']}: {counts['pages']} pages, {counts['chunks']} "
                      f"chunks, {counts['embedded']} embedded")
    except (WorkspaceError, IngestionError, EmbeddingError) as exc:  # messages carry reasons only, never text or paths
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except psycopg.errors.UniqueViolation:
        print("error: that company, user or document version already exists", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
