# ClaimsRAG — Data Specification

> **Status:** planned. The database can now record policy-document metadata (Phase 2.1, §7; written through
> `TenantDatabase` since Phase 2.3), but no policy
> documents have been downloaded, stored or ingested, and the `data/` directory does not
> exist yet.

## 1. Development corpus targets

| Item | Target |
|---|---|
| Insurers | 3–5 |
| Health | 10–15 documents |
| Motor | 5–10 documents |
| Travel | 5–10 documents |
| Home | 5–10 documents |
| **Total** | **approximately 30–50 policy documents** |

In development, each insurer stands in for one tenant (company workspace). This lets tenant isolation be tested
across real, separate corpora.

## 2. Required corpus characteristics

The corpus should eventually contain:

- multiple versions of the same policy (for freshness and version tests)
- table-rich documents
- scanned PDFs (to exercise the OCR fallback)
- original insurer PDFs, which are the source of truth (D-010)
- page-level provenance for every expected answer
- supported questions
- abstention questions
- multi-document questions
- tenant-isolation tests
- freshness/version tests

## 3. Directory layout (conceptual)

```text
data/
├── raw/          original insurer PDFs, stored unmodified
├── processed/    derived artifacts (extracted text, chunks), regenerable from raw/
└── evaluation/   golden dataset: corpus-v<N>.json (manifest), golden-v<N>.jsonl (cases), CHANGELOG.md
```

The evaluation files' format, versioning and minimum composition are specified in [EVALUATION.md](EVALUATION.md) §7
(Phase -1, D-034). The directory is created by Phase 10, not before.

`data/raw/` is the source of truth. Anything in `data/processed/` can be regenerated from it.

## 4. Per-document metadata (finalised in Phase -1)

Each document's entry in the corpus manifest (`data/evaluation/corpus-v<N>.json`, [EVALUATION.md](EVALUATION.md)
§7.1) records the following, so provenance can be checked:

- insurer and the development tenant it belongs to
- insurance type (`HEALTH`, `MOTOR`, `TRAVEL`, `HOME`)
- policy name and version label (plus the effective date, if the document states one)
- source URL and download date
- file checksum (e.g. SHA-256) and page count
- whether it is scanned or born-digital, and whether it contains significant tables
- the ClaimsRAG identifiers it is loaded under: `policy_number`, `document_key` and `version` (§7)
- whether it is `synthetic` (generated for testing, e.g. by `scripts/workspace.py sample-pdf`) or a real insurer PDF

## 5. Evaluation question types

See [EVALUATION.md](EVALUATION.md) §1, and the case types and fields of §7.2–§7.3. Every expected answer names its
document, version, page and clause, the clause as an exact quote (§7.4).

| Type | Purpose |
|---|---|
| Supported | Answer exists in the tenant's active documents. |
| Abstention | No sufficient evidence, so the system must abstain. |
| Multi-document | Evidence spans more than one document within one tenant. |
| Tenant isolation | Answer exists only in another tenant's documents, so the system must abstain and must not leak. |
| Freshness/version | Versions differ, so the answer must come from the active version. |
| Numerical/table | Answer comes from exact figures or table cells. |

## 6. Constraints

- No documents are downloaded or ingested until a phase explicitly calls for it.
- Before committing any insurer PDF to a repository, check its terms of use.

## 7. Stored document metadata (Phase 2.1)

Migration `0002` (D-024, [ARCHITECTURE.md](ARCHITECTURE.md) §2.9) records policies and their documents. It stores no
file content and holds no data until a later phase creates records.

| §4 item | Where it is recorded |
|---|---|
| insurer / tenant | `policies.company_id` and `policy_documents.company_id` (always the same company) |
| insurance type | `policies.insurance_type`: `HEALTH`, `MOTOR`, `TRAVEL`, `HOME` |
| policy name | `policies.name`; the insurer's policy number is `policies.policy_number`, unique per company |
| version label | `policy_documents.document_key` (which document of the policy, e.g. `policy-wording`) + `version` (1, 2, …) |
| file checksum | `policy_documents.checksum_sha256` (64 lower-case hex characters; integrity metadata, not unique) |
| the file itself | not stored in the database: `policy_documents.storage_reference` is an opaque reference, unique across all companies |
| other file metadata | `original_filename` (a name, never a path), `content_type` (`application/pdf`), `size_bytes` |

Not recorded yet: effective date, source URL, download date, page count, scanned/born-digital and table flags. Each
will be added by the phase that first needs it (versioning and freshness are Phase 11, extraction is Phase 3).

Policies can be created, renamed or renumbered, re-typed, activated or deactivated and deleted through
`TenantDatabase` (Phase 2.2), always within the caller's own company. A policy that still has document records cannot
be deleted.

Document records can be created, renamed (display file name only), set `UPLOADED` or `INACTIVE` and deleted
through `TenantDatabase` (Phase 2.3), always under one of the caller's own policies.
- **Versions:** the caller gives each new version's number. A recorded version's key, number, policy and file
  metadata never change (D-025).
- **Metadata only:** the checksum and storage reference are recorded as given. No file is uploaded, stored, read or
  hashed yet.
- **Over HTTP (Phase 2.4):** the policy and document API records the same metadata (ARCHITECTURE.md §2.10).
  - The client sends the checksum, size, file name, key and version.
  - The server generates the storage reference (32 random bytes, URL-safe), which is never accepted from or
    returned to the client.
  - The responses omit `company_id` and `storage_reference`.

No data has been loaded.

## 8. Stored files and extracted text (Phase 3)

- **Files:** the original PDF of a document is stored unmodified at `<storage root>/<storage_reference>`
  (`app/storage.py`, a local development adapter). The reference is the server-generated value recorded on the
  document, never a path. Files are written once.
- **Extraction output** (`app/ingestion.py`, held in memory and not persisted yet), one per document version:

  | Field | Meaning |
  |---|---|
  | `document_id`, `policy_id`, `document_key`, `version`, `original_filename` | provenance, from the tenant's record |
  | `checksum_sha256` | the SHA-256 of the exact bytes the text came from (verified against the record) |
  | `page_count`, `pages[]` | every page in PDF order: `page_number` (1 = first page) and normalised `text` (empty if the page has none) |

- **Normalisation:** line endings, control characters, runs of spaces and blank lines are normalised, and the
  text is put in NFC. The wording is otherwise unchanged. Paragraphs (text blocks) are separated by one blank
  line.
- **Not covered yet:** scanned PDFs (no text layer) fail with `NO_TEXT` until the OCR fallback exists.

## 9. Chunks (Phase 4)

Produced in memory by `app/chunking.py` from the extraction output (§8). Not persisted yet; Phase 5 consumes
them.

| Field | Meaning |
|---|---|
| `chunk_id` | UUIDv5 of `document_id/version/chunk_index/sha256(text)`: the same input always gives the same id |
| `document_id`, `policy_id`, `document_version` | the source document version |
| `chunk_index` | position in the document, from 0 |
| `page_start`, `page_end` | the PDF pages the text comes from (equal for a single-page chunk) |
| `section_heading` | the nearest heading line at or before the chunk's text, or none |
| `text` | the source paragraphs, unchanged, joined by one blank line; at most `max_chars` (default 1500) |

- **Document-level fields** (`document_key`, `original_filename`, `checksum_sha256`, `max_chars`) are on the
  enclosing `DocumentChunks`.
- **No overlap:** chunks never repeat text.

## 10. Chunk embeddings (Phase 5)

Table `chunk_embeddings` (migration `0003`): one row per chunk and embedding model, immutable, tenant-owned.

| Column | Meaning |
|---|---|
| `id` | the row id (generated) |
| `company_id` | the owning tenant (the tenant of the `TenantDatabase` that stored it) |
| `policy_id`, `document_id`, `document_version` | the source document version; the composite foreign key ties them to one `policy_documents` row of the same company |
| `chunk_id`, `chunk_index` | the Phase 4 chunk (content-derived id, D-027) |
| `page_start`, `page_end`, `section_heading` | page and section provenance, as in the chunk |
| `chunk_text` | the exact Phase 4 chunk text, which is also exactly what was embedded |
| `embedding_model` | the exact weights, e.g. `Qdrant/bge-small-en-v1.5-onnx-Q` |
| `embedding_dimension`, `embedding` | 384 and a `vector(384)` of finite values (unit length for this model) |
| `created_at` | when it was stored |

- **Not stored:** storage references, PDF bytes or anything secret.
- **Model files** (about 65 MB) are kept locally in `backend/.model_cache`, which git and Docker ignore. They are
  not data and are not committed.

## 11. Retrieval results (Phase 6)

Retrieval reads `chunk_embeddings` (§10). It stores nothing and adds no schema. Each hit returned by
`app/retrieval.py` and `POST /search` contains:

| Field | Meaning |
|---|---|
| `chunk_id`, `document_id`, `policy_id`, `document_version`, `chunk_index` | the chunk and its source version |
| `page_start`, `page_end`, `section_heading` | page and section provenance for later citations |
| `chunk_text` | the exact stored chunk text |
| `semantic_rank`, `semantic_score` | its rank and cosine similarity in the vector path, if found there |
| `keyword_rank`, `keyword_score` | its rank and `ts_rank_cd` in the full-text path, if found there |
| `hybrid_score` | its Reciprocal Rank Fusion score (the order) |

- **The result as a whole** records the query, the filters, `top_k`, `method` (`hybrid`) and the embedding
  model.
- **No company, vectors or storage references.**
- **Only `UPLOADED` documents of `ACTIVE` policies are searchable** (D-029).

## 12. Grounded answers (Phases 8–9)

`POST /chat` stores nothing: no question, answer, verdict or prompt is persisted or logged. The response contains:

| Field | Meaning |
|---|---|
| `question` | the question as asked (edge whitespace trimmed) |
| `status` | `ANSWERED`, `CONFLICT` or `ABSTAINED` (Phase 9) |
| `answer` | the verified answer; for `CONFLICT`, a fixed notice followed by what each passage says; for `ABSTAINED`, always the fixed sentence "The provided policy evidence is insufficient to determine the answer." |
| `citations` | the passages the answer rests on (Phase 9), each `{chunk_id, document_id, policy_id, document_version, page_start, page_end, section_heading}`; empty for `ABSTAINED`, at least one for `ANSWERED`, at least two for `CONFLICT` |
| `model`, `embedding_model` | the LLM (e.g. `qwen2.5:3b`) and the embedding model used |
| `generated` | whether the LLM was called (false when there was no evidence) |
| `evidence_ids`, `evidence` | the retrieved chunks given to the model, in order, with their provenance and text (§11 fields) |
| `retrieval_ms`, `generation_ms`, `output_tokens` | timings and the model's output token count |

- **Citations come from stored provenance only:** each one is a passage in `evidence`, with exactly that passage's
  stored fields, never text written by the model or sent by the client. A `[k]` in the answer refers to the k-th
  citation.
- **`evidence` lists what the model was shown;** `citations` lists the subset the verified answer rests on.
- **No internal reason:** why an answer was withheld is logged, never returned.
- **Existing clients:** every Phase 8 field keeps its name and type; `status` and `citations` were added.
- **The prompt** carries each passage's document, policy, version, pages, section and exact text. It never
  includes the company, storage references or vectors.

## 13. Sign-in and local workspaces (Frontend & Final Integration)

**`POST /auth/login`** stores nothing. Its response:

| Field | Meaning |
|---|---|
| `access_token` | the signed access token (D-017), shown to no one: the frontend keeps it in `sessionStorage` and sends it as a bearer header |
| `token_type` | always `bearer` |
| `expires_in` | seconds until the token expires (`ACCESS_TOKEN_LIFETIME_MINUTES` × 60) |
| `user` | `{full_name, email, role}` of the signed-in user, for display; never a company id, status or password hash |

**Local workspaces** are prepared with `backend/scripts/workspace.py` (D-032):
- **Companies and users:** the existing `companies` and `users` tables; passwords are stored as Argon2id hashes only.
- **Documents:** `index-document` writes the original PDF to the local document storage
  (`backend/.document_storage/`, mode 700, git-ignored), under a new 43-character server-generated reference. It
  records a `policy_documents` row (file name, size and SHA-256 of the stored bytes; key and version from the
  operator) and stores the chunks' embeddings in `chunk_embeddings`. Nothing new is persisted: no new table, column
  or migration.
- **The sample wording** (`sample-pdf`) is a synthetic, 4-page health policy wording that says on its first page
  that it is not issued by any insurer. It exists for demonstrations and the end-to-end check, and is not part of
  the development corpus (§1).
- **The demonstration workspace** used to verify this workstream (company `demo-health`, one admin, one staff
  user, policy `DH-100` with the indexed sample) lives in the local development database only. Its passwords were
  generated at random and are not recorded in the repository.

### 13.1 Registration and upload (D-035)

**`POST /auth/register-company`** creates a `companies` row (`name`, `slug`) and its first `users` row (email
normalised, Argon2id hash, `full_name`, role `COMPANY_ADMIN`) in one statement. Its 201 response:

| Field | Meaning |
|---|---|
| `company` | `{name, slug}` as stored (name trimmed, slug lower-cased) |
| `admin` | `{full_name, email, role}` of the new admin; never an id or hash |

**`POST /policies/{policy_id}/documents/upload`** stores the PDF under `DOCUMENT_STORAGE_DIR` (a new 43-character
reference), records a `policy_documents` row (with `original_filename` from `safe_filename`, the size and SHA-256 of
the stored bytes, and `document_key` and `version` from the query), and stores its chunks' embeddings in
`chunk_embeddings` (Phase 5). Its 201 response:

| Field | Meaning |
|---|---|
| `document` | the recorded version, with the §7 public fields (no `company_id`, no `storage_reference`) |
| `page_count` | pages extracted (Phase 3) |
| `chunk_count` | passages indexed (Phase 4–5) |

No new table, column or migration.

