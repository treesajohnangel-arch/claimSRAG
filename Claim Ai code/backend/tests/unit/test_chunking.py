"""Unit tests for structure-aware chunking (Phase 4). No database: the chunker is a pure function of a Phase 3
extraction result and its configuration."""

import ast
import inspect
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

import app.chunking as chunking_module
from app.chunking import (DEFAULT_MAX_CHARS, Chunk, ChunkingConfig, ChunkingError, DocumentChunks, chunk_document)
from app.ingestion import DocumentExtractionResult, ExtractedPage, extract_pages
from tests.conftest import make_pdf

DOCUMENT = uuid.UUID("11111111-1111-4111-8111-111111111111")
POLICY = uuid.UUID("22222222-2222-4222-8222-222222222222")
SMALL = ChunkingConfig(max_chars=200)


def _extraction(*pages: str, document_id: uuid.UUID = DOCUMENT, version: int = 2) -> DocumentExtractionResult:
    return DocumentExtractionResult(
        document_id=document_id, policy_id=POLICY, document_key="policy-wording", version=version,
        original_filename="Health Policy 2026.pdf", checksum_sha256="ab" * 32,
        pages=tuple(ExtractedPage(page_number=number, text=text) for number, text in enumerate(pages, start=1)))


def _texts(result: DocumentChunks) -> list[str]:
    return [chunk.text for chunk in result.chunks]


def _pages(result: DocumentChunks) -> list[tuple[int, int]]:
    return [(chunk.page_start, chunk.page_end) for chunk in result.chunks]


def _sentence(n: int) -> str:
    return f"Clause {n:02d} applies to the insured person."


# --- Grouping and structure ------------------------------------------------------------------------------------


def test_one_paragraph_is_one_chunk_with_full_provenance() -> None:
    result = chunk_document(_extraction("The insurer will pay reasonable hospital expenses."))

    (chunk,) = result.chunks
    assert (chunk.document_id, chunk.policy_id, chunk.document_version, chunk.chunk_index) == (DOCUMENT, POLICY, 2, 0)
    assert (chunk.page_start, chunk.page_end, chunk.section_heading) == (1, 1, None)
    assert chunk.text == "The insurer will pay reasonable hospital expenses."
    assert (result.document_key, result.original_filename, result.checksum_sha256, result.max_chars) == (
        "policy-wording", "Health Policy 2026.pdf", "ab" * 32, DEFAULT_MAX_CHARS)


def test_short_paragraphs_are_grouped_and_keep_their_boundary() -> None:
    result = chunk_document(_extraction("Paragraph one.\n\nParagraph two.\n\nParagraph three."))

    assert _texts(result) == ["Paragraph one.\n\nParagraph two.\n\nParagraph three."]


def test_paragraphs_are_packed_whole_and_never_split_when_they_fit() -> None:
    first, second, third = "A" * 120, "B" * 70, "C" * 100  # 120 + 2 + 70 = 192 fits in 200; adding 100 does not

    result = chunk_document(_extraction(f"{first}\n\n{second}\n\n{third}"), SMALL)

    assert _texts(result) == [f"{first}\n\n{second}", third]


def test_a_heading_starts_a_new_chunk_and_names_its_section() -> None:
    page = "\n\n".join(["Intro paragraph about the policy.", "SECTION B - EXCLUSIONS", "4. Pre-existing Diseases",
                        "Pre-existing diseases are excluded for 36 months.", "5. Maternity",
                        "Maternity is covered after 9 months.", "M" * 190])

    result = chunk_document(_extraction(page), SMALL)

    assert _texts(result) == [
        "Intro paragraph about the policy.",
        "SECTION B - EXCLUSIONS\n\n4. Pre-existing Diseases\n\nPre-existing diseases are excluded for 36 months.",
        "5. Maternity\n\nMaternity is covered after 9 months.",
        "M" * 190,
    ]
    assert [chunk.section_heading for chunk in result.chunks] == [
        None, "4. Pre-existing Diseases", "5. Maternity", "5. Maternity"]  # the last inherits its section


def test_sentence_like_lines_are_not_treated_as_headings() -> None:
    page = "\n\n".join(["2.1 The insurer will pay the claim.", "Section 4 of the Act applies.", "The Insured",
                        "10 days waiting period applies;"])

    result = chunk_document(_extraction(page))

    assert len(result.chunks) == 1 and result.chunks[0].section_heading is None


# --- Pages -----------------------------------------------------------------------------------------------------


def test_page_boundaries_are_provenance_not_chunk_boundaries() -> None:
    result = chunk_document(_extraction("Clause on page one.", "Clause on page two."))

    assert _texts(result) == ["Clause on page one.\n\nClause on page two."]
    assert _pages(result) == [(1, 2)]


def test_chunks_within_one_page_report_that_page() -> None:
    result = chunk_document(_extraction(f"{'a' * 150}\n\n{'b' * 150}", "c" * 150), SMALL)

    assert _pages(result) == [(1, 1), (1, 1), (2, 2)]


@pytest.mark.parametrize(("third_page", "expected"), [("B text on page three.", [(1, 3)]),
                                                      ("SECTION C - CLAIMS", [(1, 1), (3, 3)])],
                         ids=["grouped-across-the-gap", "separate-chunk"])
def test_an_empty_middle_page_keeps_page_numbers(third_page: str, expected: list[tuple[int, int]]) -> None:
    result = chunk_document(_extraction("A text on page one.", "", third_page))

    assert _pages(result) == expected


# --- Size, splitting and boundaries ----------------------------------------------------------------------------


def test_a_large_paragraph_is_split_at_sentence_ends() -> None:
    paragraph = " ".join(_sentence(n) for n in range(1, 31))  # 30 sentences, ~1300 characters, one paragraph

    result = chunk_document(_extraction("", paragraph), SMALL)

    assert len(result.chunks) > 1
    assert all(len(text) <= 200 for text in _texts(result))
    assert all(text.endswith(".") for text in _texts(result))
    assert " ".join(_texts(result)) == paragraph  # only the spaces at the cuts became chunk boundaries
    assert set(_pages(result)) == {(2, 2)}


def test_splitting_falls_back_to_spaces_then_to_an_exact_cut() -> None:
    words = " ".join(["indemnity"] * 60)  # no sentence end
    token = "X" * 450  # no whitespace at all

    by_space = chunk_document(_extraction(words), SMALL)
    by_cut = chunk_document(_extraction(token), SMALL)

    assert all(len(text) <= 200 and text.startswith("indemnity") and text.endswith("indemnity")
               for text in _texts(by_space))
    assert " ".join(_texts(by_space)) == words
    assert [len(text) for text in _texts(by_cut)] == [200, 200, 50]
    assert "".join(_texts(by_cut)) == token


@pytest.mark.parametrize(
    ("page", "chunks"),
    [("x" * 200, 1), ("x" * 201, 2), (f"{'a' * 99}\n\n{'b' * 99}", 1), (f"{'a' * 99}\n\n{'b' * 100}", 2)],
    ids=["block-exactly-max", "block-max-plus-one", "two-blocks-exactly-max", "two-blocks-max-plus-one"],
)
def test_the_maximum_is_inclusive(page: str, chunks: int) -> None:
    result = chunk_document(_extraction(page), SMALL)

    assert len(result.chunks) == chunks
    assert all(len(text) <= 200 for text in _texts(result))


@pytest.mark.parametrize("max_chars", [200, 333, 1500])
def test_no_chunk_ever_exceeds_the_maximum(max_chars: int) -> None:
    pages = ["\n\n".join(_sentence(n) * (n % 7 + 1) for n in range(page * 10, page * 10 + 10)) for page in range(6)]

    result = chunk_document(_extraction(*pages), ChunkingConfig(max_chars=max_chars))

    assert all(len(text) <= max_chars for text in _texts(result))


# --- Text integrity --------------------------------------------------------------------------------------------

WORDING = [
    "Policy No. POL/2026/HLT-00123-A is issued under UIN IRDAI/HLT/2026/V.1.",
    "Sum insured: ₹5,00,000 (Rupees Five Lakh); co-payment 20% applies; room rent up to 1% of SI per day.",
    "Period of insurance: 01/04/2026 to 31/03/2027, both days inclusive.",
    "Exclusions apply as stated in §4.2(a) and §4.2(b)-(c).",
    "Pre-existing diseases are covered after a waiting period; pre-\nexisting conditions must be declared.",
    "The INSURED and the Insured Person mean the same; the insured MUST notify within 48 hours.",
    "“Hospital” means an institution; 'Day Care' and \"Network Provider\" are defined in Section 2.",
]


def test_wording_numbers_symbols_quotes_and_case_are_preserved_exactly() -> None:
    pages = ["\n\n".join(WORDING[:4]), "", "\n\n".join(WORDING[4:])]

    result = chunk_document(_extraction(*pages))

    joined = "\n\n".join(_texts(result))
    for wording in WORDING:
        assert wording in joined
    assert joined == "\n\n".join(page for page in pages if page)  # nothing added, dropped or reordered


@pytest.mark.parametrize("max_chars", [200, 257, 1500])
def test_chunking_never_deletes_or_reorders_characters(max_chars: int) -> None:
    pages = ["\n\n".join(WORDING) + "\n\n" + " ".join(_sentence(n) for n in range(40)), "", "Z" * 700]

    result = chunk_document(_extraction(*pages), ChunkingConfig(max_chars=max_chars))

    assert "".join("".join(_texts(result)).split()) == "".join("".join(pages).split())


# --- Determinism and ids ---------------------------------------------------------------------------------------


def test_the_same_input_always_gives_the_same_chunks() -> None:
    pages = ("\n\n".join(WORDING), "", "SECTION C - CLAIMS\n\n" + " ".join(_sentence(n) for n in range(25)))

    first = chunk_document(_extraction(*pages), SMALL)
    again = chunk_document(_extraction(*pages), SMALL)

    assert first == again and first.model_dump() == again.model_dump()
    assert [chunk.chunk_index for chunk in first.chunks] == list(range(len(first.chunks)))


def test_chunk_ids_are_stable_unique_and_bound_to_document_and_text() -> None:
    pages = ["Alpha clause.\n\nSECTION B - TERMS\n\nBeta clause.", "Gamma clause."]
    base = chunk_document(_extraction(*pages))
    other_document = chunk_document(_extraction(*pages, document_id=uuid.uuid4()))
    other_version = chunk_document(_extraction(*pages, version=3))
    edited = chunk_document(_extraction(pages[0].replace("Beta", "Delta"), pages[1]))
    ids = [chunk.chunk_id for chunk in base.chunks]

    assert len(set(ids)) == len(ids) and all(chunk_id.version == 5 for chunk_id in ids)
    assert ids == [chunk.chunk_id for chunk in chunk_document(_extraction(*pages)).chunks]
    assert not set(ids) & {chunk.chunk_id for chunk in other_document.chunks + other_version.chunks}
    assert [a.chunk_id == b.chunk_id for a, b in zip(base.chunks, edited.chunks)] == [
        a.text == b.text for a, b in zip(base.chunks, edited.chunks)]


# --- Failure handling ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("max_chars", [0, -5, 199, 8001, "1500", True, 1500.0], ids=str)
def test_an_invalid_configuration_is_refused(max_chars: object) -> None:
    with pytest.raises(ValidationError):
        ChunkingConfig(max_chars=max_chars)  # type: ignore[arg-type]


def test_wrong_inputs_are_refused_before_chunking() -> None:
    with pytest.raises(ValidationError):
        ChunkingConfig(max_chars=500, overlap=50)  # type: ignore[call-arg]  # one parameter only
    with pytest.raises(TypeError):
        chunk_document(_extraction("Text."), {"max_chars": 500})  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        chunk_document({"pages": [{"page_number": 1, "text": "Text."}]})  # type: ignore[arg-type]


@pytest.mark.parametrize("pages", [(), ("",), ("", "", "")], ids=["no-pages", "blank-page", "blank-pages"])
def test_an_extraction_without_text_cannot_be_chunked(pages: tuple[str, ...]) -> None:
    with pytest.raises(ChunkingError):
        chunk_document(_extraction(*pages))


def _chunk(**changes: object) -> dict[str, object]:
    return {"chunk_id": uuid.uuid4(), "document_id": DOCUMENT, "policy_id": POLICY, "document_version": 2,
            "chunk_index": 0, "page_start": 1, "page_end": 1, "section_heading": None, "text": "Text."} | changes


@pytest.mark.parametrize(
    "chunk",
    [_chunk(page_start=2, page_end=1), _chunk(page_start=0), _chunk(text=""), _chunk(text=" padded "),
     _chunk(chunk_index=-1)],
    ids=["end-before-start", "page-zero", "empty-text", "edge-whitespace", "negative-index"],
)
def test_a_chunk_with_impossible_provenance_cannot_exist(chunk: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Chunk(**chunk)


@pytest.mark.parametrize(
    "chunks",
    [[], [_chunk(chunk_index=1)], [_chunk(), _chunk(chunk_index=2)], [_chunk(document_id=uuid.uuid4())],
     [_chunk(text="x" * 201)], [_chunk(page_start=3, page_end=3), _chunk(chunk_index=1)]],
    ids=["none", "index-not-from-zero", "index-gap", "other-document", "too-long", "pages-out-of-order"],
)
def test_a_document_result_with_inconsistent_chunks_cannot_exist(chunks: list[dict[str, object]]) -> None:
    with pytest.raises(ValidationError):
        DocumentChunks(document_id=DOCUMENT, policy_id=POLICY, document_key="policy-wording", document_version=2,
                       original_filename="p.pdf", checksum_sha256="ab" * 32, max_chars=200,
                       chunks=tuple(Chunk(**chunk) for chunk in chunks))


# --- Real Phase 3 output ---------------------------------------------------------------------------------------


def test_real_phase_3_extraction_is_chunked_with_its_pages() -> None:
    pdf = make_pdf(("1. Definitions", "Insured means the person named in the Schedule."), None,
                   ("2. Exclusions", "Pre-existing diseases are excluded."))
    extraction = DocumentExtractionResult(document_id=DOCUMENT, policy_id=POLICY, document_key="policy-wording",
                                          version=2, original_filename="p.pdf", checksum_sha256="ab" * 32,
                                          pages=extract_pages(pdf))

    result = chunk_document(extraction)

    assert [(chunk.text, chunk.page_start, chunk.page_end, chunk.section_heading) for chunk in result.chunks] == [
        ("1. Definitions\n\nInsured means the person named in the Schedule.", 1, 1, "1. Definitions"),
        ("2. Exclusions\n\nPre-existing diseases are excluded.", 3, 3, "2. Exclusions"),
    ]


# --- Boundary --------------------------------------------------------------------------------------------------


@pytest.mark.security
def test_the_chunker_takes_no_tenant_path_or_storage_input_and_exposes_none() -> None:
    # Its only input is the verified Phase 3 result; tenant and storage internals never appear in its output.
    assert list(inspect.signature(chunk_document).parameters) == ["extraction", "config"]
    assert set(ChunkingConfig.model_fields) == {"max_chars"}
    for model in (Chunk, DocumentChunks):
        assert not {"company_id", "tenant_id", "storage_reference", "path"} & set(model.model_fields)


@pytest.mark.security
def test_the_chunking_module_reads_no_pdf_database_storage_or_tenant() -> None:
    tree = ast.parse(Path(chunking_module.__file__).read_text(encoding="utf-8"))
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}

    # It never re-parses the PDF or reaches the database, storage, HTTP or tenant resolution.
    assert imported == {"hashlib", "re", "uuid", "pydantic", "app.ingestion"}
