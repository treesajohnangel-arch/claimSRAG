"""Structure-aware chunking (Phase 4): a Phase 3 extraction result → deterministic chunks for embedding.

    DocumentExtractionResult (pages of normalised text, paragraphs separated by one blank line)
        → paragraph blocks, each with its page number (empty pages contribute none, so numbering never shifts)
        → blocks longer than max_chars split at sentence ends, then line breaks, then spaces, then a hard cut
        → blocks packed in order into chunks of at most max_chars; a heading always starts a new chunk
        → DocumentChunks(provenance, chunks[Chunk])

- **Wording is never changed.** Chunk text is the source blocks joined by one blank line ("\\n\\n"). Only the
  whitespace at a split point is dropped. Every non-whitespace character keeps its order, and nothing is
  summarised, rewritten or re-cased.
- **No PDF access, database, tenant or path input.** The chunker consumes the already verified extraction result
  of ``ingest_document`` and nothing else.
- **Provenance per chunk:** document, policy, version, page range (page_start..page_end) and the nearest heading.
  Page boundaries record provenance, but do not force a chunk boundary.
- **Deterministic:** a pure function of the extraction and the configuration. Chunk ids are UUIDv5 values of
  (document id, version, index, SHA-256 of the chunk text).
- **No overlap:** chunks end at paragraph or heading boundaries, so overlap would mostly duplicate whole
  paragraphs, and the same wording would then be cited from two chunks.
"""

import hashlib
import re
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ingestion import DocumentExtractionResult

# The default size: about 250 words or ~375 tokens of English policy text. Typical clauses fit whole, and it stays
# under the 512-token input limit of common local embedding models (Phase 5) with room to spare.
DEFAULT_MAX_CHARS = 1500
MIN_MAX_CHARS = 200
MAX_MAX_CHARS = 8000
# Fixed namespace for chunk ids. Changing it changes every chunk id.
CHUNK_ID_NAMESPACE = UUID("5b6f0e3c-8a41-4d7e-9c2b-1f0a7d3e6c95")

_BLOCK_SEPARATOR = "\n\n"
_HEADING_MAX_CHARS = 120
_SECTION_KEYWORD = re.compile(r"(?:SECTION|PART|CHAPTER|SCHEDULE|ANNEXURE|APPENDIX|ARTICLE)\b", re.IGNORECASE)
_NUMBERED_TITLE = re.compile(r"\d+(?:\.\d+)*[.)]?\s+[A-Z]")
_SENTENCE_END = re.compile(r"(?<=[.;:!?])\s+")


class ChunkingError(Exception):
    """The extraction result cannot be chunked (it holds no text)."""


class ChunkingConfig(BaseModel):
    """The one chunking parameter: the maximum characters in a chunk."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_chars: int = Field(default=DEFAULT_MAX_CHARS, strict=True, ge=MIN_MAX_CHARS, le=MAX_MAX_CHARS)


class Chunk(BaseModel):
    model_config = ConfigDict(frozen=True)

    chunk_id: UUID
    document_id: UUID
    policy_id: UUID
    document_version: int = Field(ge=1)
    chunk_index: int = Field(ge=0)  # order within the document, from 0
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    section_heading: str | None  # the nearest heading at or before the chunk's text; None before the first one
    text: str

    @model_validator(mode="after")
    def _valid_provenance(self) -> "Chunk":
        if self.page_end < self.page_start:
            raise ValueError("page_end must not be before page_start")
        if not self.text or self.text != self.text.strip():
            raise ValueError("chunk text must be non-empty and carry no edge whitespace")
        return self


class DocumentChunks(BaseModel):
    """All chunks of one document version, in order, with the document-level provenance."""

    model_config = ConfigDict(frozen=True)

    document_id: UUID
    policy_id: UUID
    document_key: str
    document_version: int
    original_filename: str
    checksum_sha256: str
    max_chars: int
    chunks: tuple[Chunk, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> "DocumentChunks":
        if [chunk.chunk_index for chunk in self.chunks] != list(range(len(self.chunks))):
            raise ValueError("chunk_index must run 0..n-1 in order")
        for chunk in self.chunks:
            if (chunk.document_id, chunk.policy_id, chunk.document_version) != (
                    self.document_id, self.policy_id, self.document_version):
                raise ValueError("every chunk must belong to this document version")
            if len(chunk.text) > self.max_chars:
                raise ValueError("a chunk exceeds max_chars")
        if any(later.page_start < earlier.page_start for earlier, later in zip(self.chunks, self.chunks[1:])):
            raise ValueError("chunks must follow page order")
        return self


def _is_heading(block: str) -> bool:
    """A short single line that is a section heading: "SECTION B - EXCLUSIONS", "EXCLUSIONS", "4. Pre-existing
    Diseases". Only what the text itself shows; a line ending like a sentence is never a heading."""
    if "\n" in block or len(block) > _HEADING_MAX_CHARS or block.endswith((".", ",", ";", ":")):
        return False
    letters = [character for character in block if character.isalpha()]
    upper_case = len(letters) >= 3 and all(character.isupper() for character in letters)
    return bool(_SECTION_KEYWORD.match(block) or upper_case or _NUMBERED_TITLE.match(block))


def _split(block: str, limit: int) -> list[str]:
    """Split a block longer than ``limit`` into pieces of at most ``limit`` characters, at the last sentence end,
    else line break, else space within the limit, else exactly at the limit. Only the whitespace at a cut is
    dropped."""
    pieces = []
    while len(block) > limit:
        window = block[: limit + 1]  # a boundary right after the limit still leaves a piece of exactly `limit`
        cut = next((m.start() for m in reversed(list(_SENTENCE_END.finditer(window))) if 0 < m.start() <= limit),
                   None)
        if cut is None:
            cut = max(window.rfind("\n"), window.rfind(" "))
            cut = cut if cut > 0 else limit
        pieces.append(block[:cut].rstrip())
        block = block[cut:].lstrip()
    pieces.append(block)
    return pieces


def _chunk_id(document_id: UUID, version: int, index: int, text: str) -> UUID:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return uuid5(CHUNK_ID_NAMESPACE, f"{document_id}/{version}/{index}/{digest}")


def chunk_document(extraction: DocumentExtractionResult, config: ChunkingConfig | None = None) -> DocumentChunks:
    """Chunk one extracted document version. Raises ``ChunkingError`` when it holds no text."""
    if not isinstance(extraction, DocumentExtractionResult):
        raise TypeError("chunk_document takes the DocumentExtractionResult of ingest_document")
    config = ChunkingConfig() if config is None else config
    if not isinstance(config, ChunkingConfig):
        raise TypeError("config must be a ChunkingConfig")
    limit = config.max_chars

    # (page_number, text, is_heading) for every paragraph block, in document order.
    units = [(page.page_number, piece, _is_heading(block))
             for page in extraction.pages if page.text
             for block in page.text.split(_BLOCK_SEPARATOR) if block
             for piece in (_split(block, limit) if len(block) > limit else [block])]
    if not units:
        raise ChunkingError("The extraction result holds no text to chunk")

    groups: list[tuple[list[tuple[int, str, bool]], str | None]] = []
    heading: str | None = None  # the nearest heading seen so far
    current: list[tuple[int, str, bool]] = []
    current_heading: str | None = None
    for unit in units:
        _, text, is_heading = unit
        if current:
            joined = sum(len(t) for _, t, _ in current) + len(_BLOCK_SEPARATOR) * len(current) + len(text)
            if (is_heading and any(not h for _, _, h in current)) or joined > limit:
                groups.append((current, current_heading))
                current = []
        if not current:
            current_heading = heading
        if is_heading:
            heading = text
            if all(h for _, _, h in current):  # still in the chunk's leading headings
                current_heading = text
        current.append(unit)
    groups.append((current, current_heading))

    chunks = []
    for index, (group, section_heading) in enumerate(groups):
        text = _BLOCK_SEPARATOR.join(t for _, t, _ in group)
        chunks.append(Chunk(
            chunk_id=_chunk_id(extraction.document_id, extraction.version, index, text),
            document_id=extraction.document_id, policy_id=extraction.policy_id,
            document_version=extraction.version, chunk_index=index, page_start=group[0][0], page_end=group[-1][0],
            section_heading=section_heading, text=text,
        ))
    return DocumentChunks(
        document_id=extraction.document_id, policy_id=extraction.policy_id, document_key=extraction.document_key,
        document_version=extraction.version, original_filename=extraction.original_filename,
        checksum_sha256=extraction.checksum_sha256, max_chars=limit, chunks=tuple(chunks),
    )
