"""PDF ingestion (Phase 3): one tenant's recorded policy document → page-aware extracted text.

    TenantDatabase.get_document(id) → storage.read(the record's storage_reference) → size and SHA-256 check
        → PyMuPDF text blocks, page by page → safe normalisation → DocumentExtractionResult

- **Identity and source:** the document comes only from the tenant's own record (``TenantDatabase``, D-020).
  - Another tenant's id fails exactly like an unknown one (``NOT_FOUND``), and no file is read.
  - There is no company, path or storage-reference parameter. The source is always the record's server-generated
    reference, resolved by the server's storage.
- **Integrity:** the bytes must match the recorded size and checksum, so a replaced or corrupted file is never
  ingested. A PDF that MuPDF had to repair on opening (damaged, often with text missing) is refused too.
- **Text is evidence:** normalisation (``normalize_page_text``) only fixes line endings, whitespace and control
  characters. Words, punctuation, hyphenation and numbering are kept.
- **Pages:** they keep their PDF order and 1-based numbers. A page without text stays in the result (empty), so
  numbering never shifts. Text blocks are separated by a blank line, which keeps paragraph boundaries.
- **No usable text on any page** (a scanned PDF) is a failure (``NO_TEXT``: OCR needed), never an empty success.
  OCR is not implemented.
- **Read-only:** nothing is written to the database or storage, and the document's status is not changed. There is
  no chunking, embedding or persistence here (Phase 4 and later).
- **Logs** record the document id, the outcome and page counts: never text, paths or storage references.
"""

import hashlib
import logging
import re
import unicodedata
from enum import StrEnum
from uuid import UUID

import pymupdf
from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.models import DocumentStatus
from app.storage import LocalDocumentStorage, StorageError
from app.tenant_db import TenantDatabase

logger = logging.getLogger(__name__)


class IngestionFailure(StrEnum):
    NOT_FOUND = "NOT_FOUND"  # the tenant has no such document (also when another tenant does)
    INACTIVE = "INACTIVE"  # withdrawn (D-024): never ingested for retrieval
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"  # no stored file under the record's reference, or an unusable reference
    INTEGRITY_MISMATCH = "INTEGRITY_MISMATCH"  # the stored bytes differ from the recorded size or checksum
    UNREADABLE_PDF = "UNREADABLE_PDF"  # not a PDF, damaged (repaired on open), encrypted, or extraction failed
    NO_TEXT = "NO_TEXT"  # no page has usable text, e.g. a scanned PDF: OCR is required


class IngestionError(Exception):
    """Ingestion failed. ``reason`` says why; the message never contains text, paths or tenant details."""

    def __init__(self, reason: IngestionFailure) -> None:
        self.reason = reason
        super().__init__(f"Document ingestion failed: {reason}")


class ExtractedPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    page_number: int = Field(ge=1)  # 1 is the PDF's first page
    text: str  # normalised page text; empty when the page has no extractable text


class DocumentExtractionResult(BaseModel):
    """The extracted pages of one document version, with the provenance later phases keep on every chunk."""

    model_config = ConfigDict(frozen=True)

    document_id: UUID
    policy_id: UUID
    document_key: str
    version: int
    original_filename: str
    checksum_sha256: str  # of the exact bytes the text was extracted from
    pages: tuple[ExtractedPage, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def pages_without_text(self) -> tuple[int, ...]:
        """Pages left empty: candidates for the future OCR fallback."""
        return tuple(page.page_number for page in self.pages if not page.text)

    @model_validator(mode="after")
    def _pages_in_order(self) -> "DocumentExtractionResult":
        if [page.page_number for page in self.pages] != list(range(1, len(self.pages) + 1)):
            raise ValueError("pages must be numbered 1..n in order")
        return self


# --- Normalisation -----------------------------------------------------------------------------------------------

_LINE_BREAKS = re.compile(r"\r\n|\r|\u2028")
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")  # C0 controls (tab included) and DEL; not the newline
_HORIZONTAL_SPACE = re.compile(r"[ \u00a0]+")
_EXTRA_BLANK_LINES = re.compile(r"\n{3,}")


def normalize_page_text(text: str) -> str:
    """Make extracted text reliable for later processing without changing its wording.

    The text is put in Unicode NFC (canonically equivalent characters, e.g. a decomposed "é", become one form), and
    line endings become ``\\n``. Control characters become spaces, runs of spaces become one, and lines are trimmed.
    Paragraphs stay separated by exactly one blank line. Nothing else changes: no dehyphenation, case, punctuation
    or compatibility mapping (NFKC would turn "m²" into "m2").
    """
    text = unicodedata.normalize("NFC", text)
    text = _LINE_BREAKS.sub("\n", text).replace("\u2029", "\n\n")
    text = _HORIZONTAL_SPACE.sub(" ", _CONTROL.sub(" ", text))
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _EXTRA_BLANK_LINES.sub("\n\n", text).strip()


# --- Extraction --------------------------------------------------------------------------------------------------


def _page_text(page: pymupdf.Page) -> str:
    # Text blocks in the PDF's own order (no geometric re-sorting), one blank line between blocks.
    blocks = page.get_text("blocks", sort=False)
    return "\n\n".join(block[4] for block in blocks if block[6] == 0)


def extract_pages(data: bytes) -> tuple[ExtractedPage, ...]:
    """The normalised text of every page of a PDF, in order. Raises ``IngestionError`` (UNREADABLE_PDF, NO_TEXT)."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as pdf:
            # MuPDF sniffs content, so other formats (e.g. HTML) can open despite filetype="pdf": require a real PDF.
            if not pdf.is_pdf or pdf.needs_pass or pdf.is_repaired or pdf.page_count < 1:
                raise IngestionError(IngestionFailure.UNREADABLE_PDF)
            pages = tuple(ExtractedPage(page_number=number, text=normalize_page_text(_page_text(page)))
                          for number, page in enumerate(pdf, start=1))
    except IngestionError:
        raise
    except Exception as exc:
        # Untrusted bytes: the parser can fail in many ways (FileDataError, EmptyFileError, ValueError, ...).
        logger.warning("pdf extraction failed error=%s", type(exc).__name__)
        raise IngestionError(IngestionFailure.UNREADABLE_PDF) from None
    if not any(page.text for page in pages):
        raise IngestionError(IngestionFailure.NO_TEXT)
    return pages


# --- Ingestion ---------------------------------------------------------------------------------------------------


def _failed(document_id: UUID, reason: IngestionFailure) -> IngestionError:
    logger.warning("document ingestion failed document_id=%s reason=%s", document_id, reason)
    return IngestionError(reason)


def ingest_document(db: TenantDatabase, storage: LocalDocumentStorage, document_id: UUID) -> DocumentExtractionResult:
    """Extract the pages of the tenant's document ``document_id``. Raises ``IngestionError`` with the reason."""
    if not isinstance(db, TenantDatabase) or not isinstance(storage, LocalDocumentStorage):
        raise TypeError("ingest_document needs a TenantDatabase and the server's document storage")
    if not isinstance(document_id, UUID):
        raise TypeError("document_id must be a UUID")

    document = db.get_document(document_id)
    if document is None:
        raise _failed(document_id, IngestionFailure.NOT_FOUND)
    if document.status is DocumentStatus.INACTIVE:
        raise _failed(document_id, IngestionFailure.INACTIVE)
    try:
        data = storage.read(document.storage_reference)
    except StorageError:
        raise _failed(document_id, IngestionFailure.SOURCE_UNAVAILABLE) from None
    if len(data) != document.size_bytes or hashlib.sha256(data).hexdigest() != document.checksum_sha256:
        raise _failed(document_id, IngestionFailure.INTEGRITY_MISMATCH)
    try:
        pages = extract_pages(data)
    except IngestionError as exc:
        raise _failed(document_id, exc.reason) from None

    result = DocumentExtractionResult(
        document_id=document.id, policy_id=document.policy_id, document_key=document.document_key,
        version=document.version, original_filename=document.original_filename,
        checksum_sha256=document.checksum_sha256, pages=pages,
    )
    logger.info("document ingested document_id=%s pages=%d pages_without_text=%d", document.id,
                result.page_count, len(result.pages_without_text))
    return result
