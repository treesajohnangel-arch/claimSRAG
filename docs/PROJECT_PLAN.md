# ClaimsRAG — Project Plan

The master phase hierarchy and the **authoritative status of each phase**. Other documents refer here and do not
keep their own copy of phase status.

## Status values

| Status | Meaning |
|---|---|
| NOT STARTED | No work has begun. |
| IN PROGRESS | Work has begun but is not finished. |
| COMPLETE | Scope delivered and verified, with checks recorded in [IMPLEMENTATION_LOG.md](IMPLEMENTATION_LOG.md). |
| BLOCKED | Cannot proceed. The blocker is written next to the phase. |

## Phase status

The table below is the full 15-phase master roadmap (plus Phase -1). The last column shows which phases the
current academic submission implements (see "Accelerated Submission Path" below).

| Phase | Name | Status | Submission path |
|---|---|---|---|
| -1 | Evaluation & Quality Specification | COMPLETE | Done after the accelerated path (specification only) |
| 0 | Engineering Foundation | COMPLETE | Yes |
| 1 | Multi-Tenant Identity & Security | COMPLETE | Yes |
| 2 | Policy & Document Management | COMPLETE | Yes |
| 3 | PDF Ingestion Pipeline | COMPLETE | Yes |
| 4 | Structure-Aware Chunking | COMPLETE | Yes |
| 5 | Embedding Infrastructure | COMPLETE | Yes |
| 6 | Hybrid Retrieval | COMPLETE | Yes |
| 7 | Reranking | NOT STARTED | Deferred |
| 8 | Grounded Answer Engine | COMPLETE | Yes |
| 9 | Evidence Sufficiency, Abstention & Citations | COMPLETE | Yes |
| 10 | Evaluation Framework | NOT STARTED | Deferred |
| 11 | Policy Versioning & Freshness | NOT STARTED | Deferred |
| 12 | Multi-Document Reasoning | NOT STARTED | Deferred |
| 13 | Policy Schedule | NOT STARTED | Deferred |
| 14 | Production Hardening | NOT STARTED | Deferred |
| 15 | Final Integration & Deployment | IN PROGRESS | Partly: minimum final integration COMPLETE; deployment deferred |

Last updated: 2026-11-13 (Phase -1 complete: the evaluation specification; Phase 7 and Phase 10 stay deferred and wait for the project review).

## Phase rules

- Do not implement a later phase's features early.
- A phase is COMPLETE only when the checks behind that claim are recorded in the implementation log.
- From Phase 6 (Hybrid Retrieval) onward, retrieval and generation changes are measured against the golden dataset
  (see [EVALUATION.md](EVALUATION.md), *Regression principle*). Phase -1 specified that dataset and its measures
  (EVALUATION.md §6–§18); Phase 10 builds them.

## Accelerated Submission Path

The 15-phase roadmap above remains ClaimsRAG's complete architecture and long-term implementation plan. No phase
has been removed, merged or renumbered. Because of the submission deadline, the current academic submission is
built through a focused subset of those phases:

| Step | Phase | Status |
|---|---|---|
| 1 | Phase 0 — Engineering Foundation | COMPLETE |
| 2 | Phase 1 — Multi-Tenant Identity & Security | COMPLETE |
| 3 | Phase 2 — Policy & Document Management | COMPLETE |
| 4 | Phase 3 — PDF Ingestion Pipeline | COMPLETE (text-layer PDFs) |
| 5 | Phase 4 — Structure-Aware Chunking | COMPLETE (in memory) |
| 6 | Phase 5 — Embedding Infrastructure | COMPLETE (local model, pgvector) |
| 7 | Phase 6 — Hybrid Retrieval | COMPLETE (no reranking) |
| 8 | Phase 8 — Grounded Answer Engine | COMPLETE (local LLM) |
| 9 | Phase 9 — Evidence Sufficiency, Abstention & Citations | COMPLETE (verified answers and citations) |
| 10 | Final integration and submission verification (the minimum part of Phase 15) | COMPLETE (frontend, end-to-end verification) |

**Workstream 3 — Grounded RAG:** Phase 8 COMPLETE, Phase 9 COMPLETE.

**Workstream 4 — Frontend & Final Integration:** Frontend — COMPLETE, Final integration — COMPLETE.

**Why this subset:** the submission prioritises the core end-to-end value chain. This is the minimum coherent
ClaimsRAG GenAI pipeline:

```text
PDF → page-aware extraction → structure-aware chunks → embeddings → tenant-filtered retrieval
    → grounded LLM answer → evidence/page provenance → evidence sufficiency and abstention
    → final integrated demonstration
```

**Verified state of the completed steps:**
- **Phase 0:** the engineering foundation is complete.
- **Phase 1:** multi-tenant identity and security are complete.
- **Phase 2:** policy and document management (data model, database operations, API, hardening) are complete.
- **Phase 3:** page-aware PDF text extraction is complete for PDFs with a text layer.
  - Scanned or image-only PDFs are detected and refused (`NO_TEXT`), because OCR is not implemented.
  - Repaired (damaged) PDFs are deliberately refused.
  - PyMuPDF's licence (AGPL-3.0 or the Artifex commercial licence) is documented in `backend/requirements.txt`
    and the Phase 3 log.
- **Phase 4:** structure-aware chunking is complete as an in-memory, deterministic chunker (D-027).
- **Phase 5:** chunks are embedded locally and stored in tenant-scoped pgvector rows (D-028).
- **Phase 6:** hybrid retrieval (exact pgvector cosine + PostgreSQL full-text, Reciprocal Rank Fusion), all
  tenant-scoped in SQL, with policy, document and insurance-type filters and `POST /search` (`search:use`;
  D-029).
- **Phase 7:** deferred from the accelerated path. There is no reranking.
- **Phase 8:** grounded answers from the retrieved evidence, through a local LLM (qwen2.5:3b on Ollama), with
  `POST /chat` (`chat:use`; D-030).
  - The model is BAAI/bge-small-en-v1.5 (384 dimensions) through fastembed and ONNX Runtime.
  - The schema is migration `0003` (`chunk_embeddings`).
  - There is no similarity search yet.
- **Phase 9:** `/chat` answers only when a single model verdict passes deterministic verification against the
  passages it was shown, and otherwise abstains. It returns `ANSWERED`, `CONFLICT` or `ABSTAINED`, with citations
  built from stored provenance (D-031). With qwen2.5:3b, real conflicts end as abstentions (safe), not conflict
  reports.
- **Frontend & Final Integration:** the React + Vite frontend (sign-in, overview, policies and documents, evidence
  search, questions with citations; D-033) over the existing API, plus `POST /auth/login` (the one public write)
  and local operator tooling for workspaces and document indexing (D-032). The whole flow was verified end to end in a
  real browser, against the real backend, PostgreSQL and local LLM:
  sign-in → policy → document → indexed evidence → search → answer → citation → evidence.
  - **End-to-end fix (2026-11-14, D-035):** company registration (`POST /auth/register-company`) and PDF upload
    (`POST /policies/{id}/documents/upload`, indexed through `app/indexing.py`) close the first-run gaps. The
    sign-in 500 (no database settings in `backend/.env`) is fixed, and an unreachable database is now a 503. A
    new company can register, sign in, create a policy, upload its PDF, search it and get cited answers, verified
    in a real browser.
  - **Frontend redesign (2026-11-14):** a dark visual system (tokens in `frontend/src/styles/global.css`, IBM Plex
    type) with ClaimsRAG-specific evidence presentation. The API contract is unchanged.
  - **Direct opening (2026-11-14, D-036):** there is no sign-in screen. With `DEMO_COMPANY_SLUG` and
    `DEMO_USER_EMAIL` set (development only), `POST /auth/demo-session` issues a normal token for that one user and
    company, and the frontend opens straight on the dashboard. Tenant isolation, RBAC and the RAG pipeline are
    unchanged. Verified in a real browser: open → policy → upload → indexed → search → cited answer → abstention.

### Deferred from the accelerated submission path

These remain planned parts of the master roadmap, needed for a more production-ready system. They are deferred
from the accelerated submission path, and may be taken up if time permits. They are not cancelled.

- **Phase -1 — Evaluation & Quality Specification:** deferred from the path, then completed on 2026-11-13 as a
  specification (below). The runner and dataset it specifies are Phase 10.
- **Phase 7 — Reranking:** retrieval candidates are not reordered by a reranker.
- **Phase 10 — Evaluation Framework**
- **Phase 11 — Policy Versioning & Freshness**
- **Phase 12 — Multi-Document Reasoning**
- **Phase 13 — Policy Schedule**
- **Phase 14 — Production Hardening**
- **Phase 15 — Final Integration & Deployment:** only the broader production deployment and hardening work is
  deferred. Minimum integration and verification of the implemented pipeline is still required for the
  submission, as the last step above.

**Invariants still apply.** Deferring a phase does not lift the non-negotiable invariants in
[CLAUDE.md](../CLAUDE.md). Two of them touch deferred phases, and the phases that meet them must say how they
comply:
- **Invariant 8** (measure retrieval and generation changes against a golden dataset) is normally served by
  Phases -1 and 10. Phase -1 (the specification) is complete; Phase 10 (the runner and golden dataset) is
  deferred. Until it is built, the interim measurements of [EVALUATION.md](EVALUATION.md) §17 are the baseline.
- **Invariant 6** (current retrieval uses validated `ACTIVE` versions) is normally served by Phase 11; documents
  are only `UPLOADED` or `INACTIVE` today.

**Settled in Phase 6 (D-029), as interim rules until the deferred phases arrive:**
- **Invariant 6:** retrieval searches only `UPLOADED` documents of `ACTIVE` policies. Withdrawn versions
  (`INACTIVE`) and inactive policies are never returned.
- **Invariant 8:** a fixed retrieval regression set (`tests/database/test_retrieval_quality.py`) runs the real
  model and database over a fixed corpus and question set. Its measured baseline is recall@1 = 5/6 and
  recall@3 = 6/6, and every retrieval change must keep it, or change it on purpose. It is not the full golden
  dataset of Phase -1/10.

## Phase scope

Each phase below has a short scope statement. A detailed task brief is written when the phase starts. Nothing here
is implemented unless the phase is marked COMPLETE above.

### Phase -1 — Evaluation & Quality Specification

Specify how quality will be measured before features are built. This covers the golden dataset format, question
categories, metric definitions and acceptance thresholds. The methodology is in [EVALUATION.md](EVALUATION.md) and
the corpus in [DATA_SPECIFICATION.md](DATA_SPECIFICATION.md). This phase is specification only. The evaluation
framework code is Phase 10.

**Status: COMPLETE (2026-11-13; D-034).** [EVALUATION.md](EVALUATION.md) §6–§18 specify:
- the golden dataset: files, versioning, case fields and types, the v1 minimum composition, and quote-based
  relevance;
- retrieval Recall@1/3/5/8;
- the answer rubric;
- citation, abstention and conflict scoring;
- security and grounding gates, which fail a run outright;
- latency measurement (targets TBD);
- the run protocol and comparison rules;
- the current baseline, separating measured from not yet measured.

No code, schema, dependency or behaviour changed. Phase 7 and Phase 10 remain deferred.

### Phase 0 — Engineering Foundation

The repository foundation. It starts with persistent project context and documentation, then adds the engineering
skeleton.

| Task | Description | Status |
|---|---|---|
| 0.1 | Repository inspection and persistent project context (documentation only) | COMPLETE |
| 0.2 | Minimal FastAPI backend foundation: app structure, configuration, `/health`, error-handling foundation, pytest | COMPLETE |
| 0.2a | Dependency verification: test-client dependency (`httpx2`) | COMPLETE |
| 0.3 | Docker Compose foundation: backend container plus PostgreSQL 18 with the pgvector extension enabled (no application schema) | COMPLETE |
| 0.4 | Configuration & environment: explicit app/server/database/secret settings, `development`/`test`/`production`, production secret validation, tests isolated from `.env` | COMPLETE |
| 0.5 | Testing foundation: unit/API test layers, shared fixtures, `database`/`security` markers, network guard for the default suite | COMPLETE |
| 0.6 | Logging & error handling: `LOG_LEVEL`, stderr logging with secret redaction, metadata-only request log, single-log 500 boundary | COMPLETE |
| 0.7 | Foundation verification: scope audit, backend/config/testing/logging/Docker/pgvector/persistence/host checks, dependency and hygiene audit, documentation consistency | COMPLETE |

This is the authoritative Phase 0 sequence. Frontend scaffolding is **not** part of Phase 0 and has not been
scheduled. (The Task 0.3 and 0.4 log entries wrongly named it as the next task.)

### Phase 1 — Multi-Tenant Identity & Security

Authentication, the `COMPANY_ADMIN` and `STAFF` roles with RBAC, tenant resolution from authenticated server-side
context, and enforced tenant scoping. Requirements are in [SECURITY.md](SECURITY.md).

| Task | Description | Status |
|---|---|---|
| 1.1 | User & company data model: `companies` and `users` tables (Alembic migration), database integrity tests | COMPLETE |
| 1.2 | Authentication: Argon2id password hashing, canonical email normalisation, `authenticate()` service (company slug + email + password) returning a safe identity | COMPLETE |
| 1.3 | Session/JWT handling: RS256 JWT access tokens (`issue_access_token()` / `decode_access_token()`), key configuration, 15-minute default lifetime; no refresh tokens or revocation | COMPLETE |
| 1.4 | RBAC: static role-to-permission mapping (`app/rbac.py`, default deny), `require_permission()`, HTTP 401/403 dependencies (`app/api/authorization.py`); no tenant authorization | COMPLETE |
| 1.5 | Tenant resolution: `TokenIdentity.company_id` as the only tenant source (`app/tenancy.py` `resolve_tenant()`, `app/api/tenancy.py` `current_tenant`); client company ids ignored; fail closed; no data-access enforcement | COMPLETE |
| 1.6 | Tenant-aware database access: `TenantDatabase(conn, TenantIdentity)` (`app/tenant_db.py`) binds `TenantIdentity.company_id` to every tenant-owned query; fail closed; application-level enforcement, no RLS | COMPLETE |
| 1.7 | API authorization: protected routes declare `permission_required` (startup guard `routes_missing_permission`, public allowlist `/`, `/health`); RBAC denials anywhere in a request are 403; permission matrix unchanged | COMPLETE |
| 1.8 | Cross-tenant security verification: database-backed adversarial matrix over the whole chain, structural boundary tests, startup route-guard blind spots fixed (D-022) | COMPLETE |
| 1.9 | Final security verification: independent review of the whole Phase 1 boundary, final attack matrix (27 classes), permission-before-other-work rule (D-023), served-app and environment-independence checks; Phase 1 exit criteria met | COMPLETE |

### Phase 2 — Policy & Document Management

Policies per company and insurance type, policy and document records, and upload of the original PDFs. A new upload
creates a new version and never overwrites an existing one (see [ARCHITECTURE.md](ARCHITECTURE.md)).

| Task | Description | Status |
|---|---|---|
| 2.1 | Policy & document data model: `policies` and `policy_documents` (migration `0002`), the database-enforced same-company rule (D-024), per-company policy numbers, versioned document records with an opaque storage reference; tenant-scoped reads in `TenantDatabase` | COMPLETE |
| 2.2 | Policy management database operations: `TenantDatabase.create_policy`, `update_policy`, `set_policy_status`, `delete_policy`, with the company taken only from `TenantIdentity`, tenant-scoped UPDATE/DELETE in the SQL itself, and deletes refused while documents exist; no API | COMPLETE |
| 2.3 | Policy document management database operations: `TenantDatabase.create_document`, `update_document` (file name only), `set_document_status` (`UPLOADED`/`INACTIVE`), `delete_document`; the policy checked against the tenant by the composite foreign key, versions given and never changed in place (D-025), metadata only; no API, storage or upload | COMPLETE |
| 2.4 | Policy & document API layer: `app/api/policies.py` (11 routes, `policy:read` / `policy:manage`, permission first), tenant from the token only, one transaction per request committed before the response (`app/api/database.py`), explicit request/response schemas without `company_id` or `storage_reference`, server-generated storage references, foreign = missing (404), database refusals mapped to 409/422 (D-026); document metadata only, no upload | COMPLETE |
| 2.5 | Policy & document API security hardening: review of the 2.4 boundary (no behaviour change needed); focused tests for tenant injection on document routes, provenance through the status route, authentication before validation, and the single transaction owner | COMPLETE |

### Phase 3 — PDF Ingestion Pipeline

Text and layout extraction with PyMuPDF, an OCR fallback for scanned pages, and page-level provenance.

**Status: COMPLETE (2026-11-13), for PDFs with a text layer.** What was built:
- `app/ingestion.py` `ingest_document`: tenant-scoped, page-aware text extraction (PyMuPDF 1.28.2), with
  provenance (document, policy, key, version, file name, checksum, page number);
- the size and checksum check, and refusal of non-PDF, encrypted and repaired PDFs;
- safe normalisation;
- `NO_TEXT` for scans;
- `app/storage.py`, a local, write-once storage adapter addressed only by server references.

The OCR fallback was **not** built: scanned PDFs fail with `NO_TEXT`, and pages without text are listed. It is
the next ingestion enhancement. (Upload over HTTP came with D-035, 2026-11-14; extractions are still not
persisted.)

### Phase 4 — Structure-Aware Chunking

Chunks follow the policy's structure (sections, clauses, tables) instead of fixed-size windows. Each chunk keeps
its clause, page and version provenance.

**Status: COMPLETE (2026-11-13), in memory.** `app/chunking.py` `chunk_document` turns a Phase 3
`DocumentExtractionResult` into deterministic `DocumentChunks` (D-027):
- **Structure:** paragraph blocks packed in order up to `max_chars` (default 1500), and a detected heading always
  starts a chunk. Oversized paragraphs are split at sentence ends, then line breaks, then spaces, then a hard
  cut.
- **No overlap,** and wording is never changed.
- **Provenance per chunk:** document, policy, version, page range and nearest heading.
- **Ids:** UUIDv5 over the document, version, index and a hash of the text.

Chunks are not persisted, there is no table handling, and nothing is embedded (Phase 5).

### Phase 5 — Embedding Infrastructure

A configurable embedding provider. Vectors are stored in pgvector together with their tenant and version context.

**Status: COMPLETE (2026-11-13).** What was built (D-028):
- **`app/embeddings.py`:**
  - `FastEmbedProvider`: BAAI/bge-small-en-v1.5, 384 dimensions, run locally from `backend/.model_cache`, with
    no network at load or inference;
  - vector validation;
  - `embed_document_chunks`: each chunk's exact text is embedded, then stored in the caller's one transaction.
- **Migration `0003`:** `chunk_embeddings`, with a `vector(384)` column. A composite provenance foreign key ties
  company, policy, document and version to the document record.
- **Idempotent:** at most one embedding per chunk and model, and a different chunking of the same document is
  refused.
- **`TenantDatabase`:** insert, list, get and delete of embeddings.

There is no vector index or similarity search (Phase 6).

### Phase 6 — Hybrid Retrieval

Vector retrieval and PostgreSQL full-text keyword retrieval, both filtered by tenant and insurance type. Their
candidates are merged and deduplicated.

**Status: COMPLETE (2026-11-13), without reranking (D-029):**
- **`app/retrieval.py`:** query embedding (same model, query text only), Reciprocal Rank Fusion (k = 60) and
  deterministic ties.
- **`TenantDatabase.search_chunks_by_vector` / `search_chunks_by_keywords`:** exact cosine search and
  `websearch_to_tsquery`, tenant-scoped across all joined tables, with optional policy, document and
  insurance-type filters.
- **`POST /search`:** behind `search:use`.
- **No migration**, and no OpenSearch or external store.

### Phase 7 — Reranking

A configurable reranker orders the merged candidates and produces the evidence set.

### Phase 8 — Grounded Answer Engine

Answers are generated from the evidence set alone, through the LLM provider boundary. The first provider is a
local LLM through Ollama.

**Status: COMPLETE (2026-11-13), on the accelerated path without Phase 7 (D-030):**
- **`app/llm.py`:** `OllamaProvider`, the local server only (URL validated as local), with no proxies or
  redirects; temperature 0, fixed seed and context; typed errors.
- **`app/answering.py`:**
  - evidence formatted deterministically inside BEGIN/END markers tagged with a hash of the evidence;
  - grounding rules in the system prompt;
  - an evidence budget;
  - no model call without evidence.
- **`POST /chat`:** behind `chat:use`. Only server-side retrieval supplies evidence.
- **Not built here:** formal evidence sufficiency, abstention and citations (added in Phase 9, below).

### Phase 9 — Evidence Sufficiency, Abstention & Citations

An evidence sufficiency check runs before generation. The system abstains when evidence is insufficient, and
citations are built and validated.

**Status: COMPLETE (2026-11-13), on the accelerated path without Phase 7 (D-031):**
- **`app/grounding.py`:** `answer_with_citations`.
  - No evidence → abstain before any model call.
  - Otherwise one model call returns a JSON verdict (answer, passage labels, status) constrained by a fixed schema.
    Judging and answering share that call, so there is no second sufficiency call.
  - `verify()` then checks the verdict deterministically against the passages shown: issued labels only, cited
    support (two passages for a conflict), no model-written citations, and every number grounded. Any failure
    abstains.
- **Citations:** built only from the stored provenance of the cited chunks (chunk, document, policy, version,
  pages, section).
- **`POST /chat`:** adds `status` and `citations`. The request body is unchanged, and the abstention reason is
  never returned.
- **No migration:** citations are computed per request.
- **Measured:** the verdict wording was chosen on 11 fixed real-model cases (10/11). Real conflicts abstain with
  qwen2.5:3b.

### Phase 10 — Evaluation Framework

Implements the methodology in [EVALUATION.md](EVALUATION.md): a golden dataset runner, metric computation and
regression reports.

### Phase 11 — Policy Versioning & Freshness

The version lifecycle and freshness guarantees. A version is validated before it becomes active, versions move
between `ACTIVE` and `SUPERSEDED`, current retrieval uses only validated active versions, historical versions stay
accessible for audit, and freshness has its own tests. Phase 2's no-overwrite storage is the base for this work.
The Phase 2 brief will confirm exactly how the work divides between Phase 2 and Phase 11.

### Phase 12 — Multi-Document Reasoning

Answers that need evidence from more than one document or policy within the same tenant. Each part of the answer
is cited.

### Phase 13 — Policy Schedule

The scope will be specified when the phase starts. No earlier phase implements it.

### Phase 14 — Production Hardening

Hardening against the threats in [SECURITY.md](SECURITY.md), plus robustness and operational readiness.

### Phase 15 — Final Integration & Deployment

End-to-end integration and deployment with Docker / Docker Compose.

**Status: IN PROGRESS.** The minimum part for the submission is complete (Frontend & Final Integration, 2026-11-13):
the frontend, sign-in, operator tooling, and the verified end-to-end flow. Deployment is deferred:
- the frontend served behind the backend's origin;
- the Compose stack wired to Ollama and the embedding model;
- production configuration.

## Out of scope for V1

- Bill auditing ([DECISIONS.md](DECISIONS.md), D-008).
