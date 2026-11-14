"""The operator tooling's boundaries (scripts/workspace.py, D-032), without a database: it is never part of the served
application, never takes a password on the command line, and its sample wording ingests with section headings."""

import ast
from pathlib import Path

import pytest

from app.chunking import chunk_document
from app.ingestion import DocumentExtractionResult, extract_pages
from scripts import workspace

APP_DIR = Path(__file__).resolve().parents[2] / "app"


@pytest.mark.security
def test_the_application_never_imports_the_operator_scripts() -> None:
    for path in APP_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                     else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any(name == "scripts" or name.startswith("scripts.") for name in names), path


@pytest.mark.security
def test_a_password_is_never_a_command_line_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        workspace.main(["create-user", "--company", "c", "--email", "e@x.example", "--name", "N", "--role", "STAFF",
                        "--password", "secret-on-the-command-line"])

    assert "unrecognized arguments: --password" in capsys.readouterr().err


def test_the_sample_wording_is_marked_synthetic_and_chunks_by_section(tmp_path: Path) -> None:
    out = tmp_path / "sample.pdf"
    assert workspace.main(["sample-pdf", "--out", str(out)]) == 0

    pages = extract_pages(out.read_bytes())
    extraction = DocumentExtractionResult(
        document_id="00000000-0000-4000-8000-000000000001", policy_id="00000000-0000-4000-8000-000000000002",
        document_key="policy-wording", version=1, original_filename="sample.pdf", checksum_sha256="0" * 64,
        page_count=len(pages), pages=pages)
    chunks = chunk_document(extraction).chunks

    assert len(pages) == 4 and "not issued by any insurer" in " ".join(pages[0].text.split())
    assert {(chunk.page_start, chunk.section_heading) for chunk in chunks} >= {
        (2, "SECTION 2 - COVERAGE"), (3, "SECTION 3 - WAITING PERIODS"), (4, "SECTION 4 - EXCLUSIONS")}
