"""Unit tests for PDF ingestion (Phase 3): normalisation, page extraction, the local storage adapter and the service
boundary. No database: tests/database/test_ingestion.py runs ingest_document against PostgreSQL."""

import ast
import inspect
import os
import secrets
from pathlib import Path

import pymupdf
import pytest

import app.ingestion as ingestion_module
from app.ingestion import (DocumentExtractionResult, ExtractedPage, IngestionError, IngestionFailure, extract_pages,
                           ingest_document, normalize_page_text)
from app.storage import STORAGE_FAILURE_MESSAGE, LocalDocumentStorage, StorageError
from tests.conftest import make_pdf, make_scanned_pdf


def _reference() -> str:
    return secrets.token_urlsafe(32)  # the format app.api.policies generates


# --- Normalisation ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Line one\r\nLine two\rLine three", "Line one\nLine two\nLine three"),
        ("The  insurer\t\twill   pay ", "The insurer will pay"),
        ("Sum\u00a0\u00a0insured", "Sum insured"),
        ("  \n  Clause 1  \n  ", "Clause 1"),
        ("Para one\n\n\n\n\nPara two", "Para one\n\nPara two"),
        ("a\x00b\x07c\x0cd", "a b c d"),
        ("Cafe\u0301", "Café"),
        ("One\u2028Two\u2029Three", "One\nTwo\n\nThree"),
    ],
    ids=["line-endings", "repeated-spaces-and-tabs", "nbsp", "edge-whitespace", "extra-blank-lines", "control-chars",
         "nfc", "unicode-separators"],
)
def test_normalisation_fixes_only_layout_characters(raw: str, expected: str) -> None:
    assert normalize_page_text(raw) == expected


@pytest.mark.parametrize(
    "wording",
    [
        "Sum insured: ₹5,00,000 (Five lakh); co-payment 20%. Exclusions apply—see §4.2(a).",
        "Pre-\nexisting diseases are covered after 36 months.",  # no dehyphenation
        "Room rent up to 1% of SI/day; ICU 2%; area 10 m² — NOT covered: cosmetic treatment.",
        "1. Definitions\n(a) \"Insured\" means the person named in the Schedule.\n(b) 'Hospital' means …",
    ],
    ids=["amounts-and-symbols", "hyphenated-line-break", "superscript-and-case", "numbering-and-quotes"],
)
def test_normalisation_never_changes_policy_wording(wording: str) -> None:
    assert normalize_page_text(wording) == wording


# --- Extraction ------------------------------------------------------------------------------------------------


def test_a_one_page_pdf_is_extracted_as_page_one() -> None:
    pages = extract_pages(make_pdf("Section 1. Cover\nThe insurer will pay hospital expenses."))

    assert pages == (ExtractedPage(page_number=1, text="Section 1. Cover\nThe insurer will pay hospital expenses."),)


def test_a_multi_page_pdf_keeps_page_order_and_boundaries() -> None:
    pages = extract_pages(make_pdf("PAGE-ONE Definitions", "PAGE-TWO Benefits", "PAGE-THREE Exclusions"))

    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text for page in pages] == ["PAGE-ONE Definitions", "PAGE-TWO Benefits", "PAGE-THREE Exclusions"]


def test_paragraph_boundaries_within_a_page_are_kept() -> None:
    pages = extract_pages(make_pdf(("1. Definitions\nInsured means the person named.",
                                    "2. Exclusions\nPre-existing conditions are excluded.")))

    assert pages[0].text == ("1. Definitions\nInsured means the person named.\n\n"
                             "2. Exclusions\nPre-existing conditions are excluded.")


def test_extraction_is_deterministic() -> None:
    data = make_pdf(("Clause A", "Clause B"), "Clause C", None, "Clause D")

    assert extract_pages(data) == extract_pages(data) == extract_pages(bytes(data))


def test_a_page_without_text_keeps_its_number() -> None:
    pages = extract_pages(make_pdf("Page one text", None, "Page three text"))
    result = DocumentExtractionResult(document_id="00000000-0000-4000-8000-000000000001",
                                      policy_id="00000000-0000-4000-8000-000000000002", document_key="policy-wording",
                                      version=1, original_filename="p.pdf", checksum_sha256="0" * 64, pages=pages)

    assert [(page.page_number, page.text) for page in pages] == [(1, "Page one text"), (2, ""), (3, "Page three text")]
    assert (result.page_count, result.pages_without_text) == (3, (2,))


@pytest.mark.parametrize("data", [make_pdf(None, None), make_pdf("   ", "\t\n"), make_scanned_pdf()],
                         ids=["blank-pages", "whitespace-only", "scanned-images"])
def test_a_pdf_without_usable_text_is_a_no_text_failure_not_an_empty_success(data: bytes) -> None:
    with pytest.raises(IngestionError) as excinfo:
        extract_pages(data)

    assert excinfo.value.reason is IngestionFailure.NO_TEXT


def _encrypted_pdf() -> bytes:
    with pymupdf.open(stream=make_pdf("secret wording"), filetype="pdf") as pdf:
        return pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="user", owner_pw="owner")


@pytest.mark.parametrize(
    "data",
    [b"", b"not a pdf at all", b"%PDF-1.7\n%garbage without objects", b"<html><body>policy</body></html>",
     make_pdf(("1. Definitions\nInsured means the person named.", "2. Exclusions\nNot covered."))[:-300],
     _encrypted_pdf()],
    ids=["empty", "text", "pdf-header-only", "html", "truncated", "encrypted"],
)
def test_malformed_damaged_or_encrypted_pdfs_are_unreadable(data: bytes) -> None:
    # A truncated PDF that MuPDF can still open is repaired with text missing: refused, never partially ingested.
    with pytest.raises(IngestionError) as excinfo:
        extract_pages(data)

    assert excinfo.value.reason is IngestionFailure.UNREADABLE_PDF


def test_an_extraction_error_inside_the_library_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_get_text(self: pymupdf.Page, *args: object, **kwargs: object) -> None:
        raise RuntimeError("internal library failure")

    monkeypatch.setattr(pymupdf.Page, "get_text", broken_get_text)

    with pytest.raises(IngestionError) as excinfo:
        extract_pages(make_pdf("Some text"))

    assert excinfo.value.reason is IngestionFailure.UNREADABLE_PDF
    assert "internal library failure" not in str(excinfo.value)


# --- Local storage ---------------------------------------------------------------------------------------------


def test_storage_round_trip_and_files_are_written_once(tmp_path: Path) -> None:
    storage, reference = LocalDocumentStorage(tmp_path / "documents"), _reference()

    storage.write(reference, b"%PDF original")
    with pytest.raises(StorageError):
        storage.write(reference, b"%PDF replacement")

    assert storage.read(reference) == b"%PDF original"
    assert sorted(path.name for path in (tmp_path / "documents").iterdir()) == [reference]


@pytest.mark.security
@pytest.mark.parametrize(
    "reference",
    ["../" + "a" * 40, "/etc/passwd", "a" * 20 + "/" + "b" * 20, "a" * 40 + ".pdf", "..\\" + "a" * 40,
     "C:" + "a" * 40, "a" * 31, "a" * 129, "a" * 40 + "\n", "", None, 12345],
    ids=["parent-dir", "absolute", "subdir", "extension", "windows-parent", "drive", "too-short", "too-long",
         "newline", "empty", "none", "not-text"],
)
def test_storage_refuses_anything_but_a_server_reference_before_touching_the_filesystem(
    tmp_path: Path, reference: object
) -> None:
    root = tmp_path / "documents"
    storage = LocalDocumentStorage(root)

    with pytest.raises(StorageError) as read_error:
        storage.read(reference)  # type: ignore[arg-type]
    with pytest.raises(StorageError):
        storage.write(reference, b"%PDF planted")  # type: ignore[arg-type]

    assert not root.exists()  # not even the root was created
    assert sorted(path.name for path in tmp_path.iterdir()) == []
    assert str(read_error.value) == STORAGE_FAILURE_MESSAGE and str(tmp_path) not in str(read_error.value)


@pytest.mark.security
def test_storage_never_follows_a_link_or_reads_a_non_file(tmp_path: Path) -> None:
    root = tmp_path / "documents"
    storage = LocalDocumentStorage(root)
    storage.write(_reference(), b"%PDF a real file")  # creates the root
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF outside the root")
    link, directory, fifo = _reference(), _reference(), _reference()
    (root / link).symlink_to(outside)
    (root / directory).mkdir()
    os.mkfifo(root / fifo)

    for reference in (link, directory, fifo, _reference()):  # a link, a directory, a FIFO, a missing file
        with pytest.raises(StorageError) as excinfo:
            storage.read(reference)
        assert str(excinfo.value) == STORAGE_FAILURE_MESSAGE


# --- Service boundary ------------------------------------------------------------------------------------------


@pytest.mark.security
def test_ingestion_takes_no_company_path_or_storage_reference_from_its_caller() -> None:
    # The source is always the tenant's own record's reference, resolved by the server's storage.
    assert list(inspect.signature(ingest_document).parameters) == ["db", "storage", "document_id"]
    assert list(inspect.signature(LocalDocumentStorage).parameters) == ["root"]
    for wrong in ((object(), LocalDocumentStorage(Path("/nonexistent")), "id"),):
        with pytest.raises(TypeError):
            ingest_document(*wrong)  # type: ignore[arg-type]


@pytest.mark.security
def test_ingestion_module_has_no_sql_http_or_tenant_construction() -> None:
    tree = ast.parse(Path(ingestion_module.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    # Data access only through TenantDatabase (D-020); no web framework, driver, token or tenant construction.
    assert not {"psycopg", "fastapi", "starlette", "app.tokens", "app.tenancy"} & imported
    assert not {"TenantIdentity", "resolve_tenant", "open"} & names
    assert not {"execute", "cursor", "_fetch", "_conn"} & attributes
