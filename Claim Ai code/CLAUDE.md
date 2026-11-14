# CLAUDE.md — ClaimsRAG

Working instructions for every Claude Code session in this repository. Read this file first. Then read the
`docs/` file for the area you are working on before you change anything.

## Project

ClaimsRAG is a private, per-company insurance policy assistant. Each insurance company has its own isolated
workspace. Authorized staff upload policy PDFs there and ask questions about those policies.

- **V1 users (roles):** `COMPANY_ADMIN`, `STAFF`
- **Insurance types:** `HEALTH`, `MOTOR`, `TRAVEL`, `HOME`

**Core requirement**

> Answers must be grounded in the authorized company's policy documents and must provide evidence/provenance.
> If sufficient evidence is unavailable, the system must abstain rather than invent an answer.

**Not in V1:** bill/claim auditing. Policy Schedule is planned for Phase 13 and must not be built earlier.

## Current state

- **Phase 0 (Engineering Foundation) is COMPLETE.**
- **Phase 1 (Multi-Tenant Identity & Security) is COMPLETE.** Tasks 1.1 (user & company data model), 1.2
  (authentication service), 1.3 (JWT access tokens), 1.4 (RBAC), 1.5 (tenant resolution), 1.6 (tenant-aware
  database access), 1.7 (API authorization), 1.8 (cross-tenant security verification) and 1.9 (final security
  verification) are complete.
- **Phase 2 (Policy & Document Management) is COMPLETE.** Tasks 2.1 (policy & document data model), 2.2
  (policy management database operations), 2.3 (policy document management database operations), 2.4 (policy &
  document API layer) and 2.5 (API security hardening) are complete.
- **Phase 3 (PDF Ingestion Pipeline) is COMPLETE** (text PDFs; the OCR fallback is not built yet).
- **Phase 4 (Structure-Aware Chunking) is COMPLETE**: `app/chunking.py` `chunk_document` produces
  deterministic in-memory chunks with page and section provenance (D-027). Chunks are not persisted.
- **Phase 5 (Embedding Infrastructure) is COMPLETE**: `app/embeddings.py` embeds chunks with a local model
  (BAAI/bge-small-en-v1.5, 384 dimensions, fastembed/ONNX) into tenant-scoped `chunk_embeddings` (migration
  `0003`, D-028). There is no similarity search yet.
- **Phase 6 (Hybrid Retrieval) is COMPLETE**: `app/retrieval.py` with the tenant-scoped
  `TenantDatabase.search_chunks_by_vector` / `search_chunks_by_keywords`, fused by Reciprocal Rank Fusion, and
  `POST /search` (`search:use`). There is no reranking (D-029).
- **Phase 7 (Reranking)** is deferred from the accelerated submission path.
- **Phase 8 (Grounded Answer Engine) is COMPLETE**: `app/answering.py`, `app/llm.py` (local Ollama,
  qwen2.5:3b) and `POST /chat` (`chat:use`) answer from the tenant's retrieved evidence (D-030).
- **Phase 9 (Evidence Sufficiency, Abstention & Citations) is COMPLETE**: `app/grounding.py`
  `answer_with_citations` verifies one JSON model verdict deterministically. `POST /chat` returns `status`
  (`ANSWERED`/`CONFLICT`/`ABSTAINED`) and `citations` built from stored provenance (D-031). There is no migration.
- **Frontend & Final Integration is COMPLETE** (the last step of the accelerated path; D-032, D-033):
  - `POST /auth/login` (`app/api/auth.py`) issues tokens for API clients.
  - `frontend/` is the React + Vite application: overview, policies and documents, evidence search, and questions
    with citations. Since D-036 it has no sign-in screen.
  - `backend/scripts/workspace.py` is local operator tooling: companies, users, indexing a PDF, and a sample wording.
- **Phase -1 (Evaluation & Quality Specification) is COMPLETE** (D-034): `docs/EVALUATION.md` §6–§18 specify:
  - the golden dataset format;
  - Recall@1/3/5/8, and answer, citation, abstention and conflict scoring;
  - the security and grounding gates (they fail a run; never averaged);
  - latency (targets TBD);
  - the comparison rules;
  - the baseline (§17: measured vs not yet measured).

  The specification only: no runner, no dataset, and no code changed.
- **The first-run product flow works end to end (2026-11-14; D-035):**
  - `POST /auth/register-company` creates a company and its first COMPANY_ADMIN;
  - `POST /policies/{id}/documents/upload` stores and indexes a PDF (`app/indexing.py`);
  - the frontend has an Upload screen (its Register screen was removed by D-036);
  - an unreachable database is a 503, request bodies are capped (413), and the sign-in 500 (a `backend/.env`
    without database settings) is fixed.
- **The frontend redesign (dark visual system, IBM Plex type) is done, and ClaimsRAG opens directly (2026-11-14;
  D-036):**
  - with `DEMO_COMPANY_SLUG` and `DEMO_USER_EMAIL` set (development only), `POST /auth/demo-session` issues a
    normal token for that one user and company;
  - the frontend opens straight on the dashboard: no login or registration screen, token in memory only;
  - tenant isolation, RBAC and the RAG pipeline are unchanged.
- **Next:** none selected. Phase 7 and Phase 10 stay deferred and wait for the project review. A retrieval or
  generation change is judged by `docs/EVALUATION.md` §16. See `docs/PROJECT_PLAN.md`.
- **The repository contains documentation, the backend in `backend/`, the frontend in `frontend/`, and a Docker Compose stack.**
  - The backend has service-info (`/`) and `/health` endpoints, configuration (including PostgreSQL settings),
    logging, an error-handling foundation, and a pytest suite.
  - The Compose stack runs the backend and PostgreSQL 18 with the pgvector extension enabled.
  - The schema has `companies` and `users` (Alembic migration `0001`), and `policies` and `policy_documents`
    (migration `0002`, Phase 2.1), and `chunk_embeddings` (migration `0003`, Phase 5). Only the policy and
    document API (Phase 2.4) queries it at request time.
  - Authentication exists as an internal service: `app/auth.py`, with `app/passwords.py` and `app/emails.py`.
  - Access tokens exist as an internal service: `app/tokens.py` issues and verifies RS256 JWTs.
  - RBAC exists:
    - `app/rbac.py` maps roles to permissions;
    - `app/api/authorization.py` has the FastAPI 401/403 dependencies.

    The policy and document API uses them.
  - Tenant resolution exists: `app/tenancy.py` and the FastAPI dependency `app/api/tenancy.py` `current_tenant`.
    The policy and document API uses it.
  - Tenant-aware database access exists: `app/tenant_db.py` `TenantDatabase`.
    - Reads: tenant-scoped reads of users, policies and policy documents.
    - Writes (Phase 2.2): `create_policy`, `update_policy`, `set_policy_status` and `delete_policy`.
    - Document writes (Phase 2.3): `create_document`, `update_document` (file name only), `set_document_status`
      and `delete_document`. They record metadata only: no file is stored, read or hashed. There are no user
      writes.

    API routes get it through `app/api/database.py` `TenantDb`: one connection and one transaction per request
    (Phase 2.4).
  - API authorization is enforced at startup: `create_app()` refuses any route without a declared permission, except
    GET/HEAD on `PUBLIC_PATHS` and FastAPI's documentation pages.
  - Cross-tenant isolation is verified end to end (Phase 1.8): an adversarial matrix against real data, plus
    structural tests on the source.
  - Phase 1.9 re-reviewed the whole boundary as the security gate before Phase 2 (`docs/SECURITY.md` §10).
  - The policy and document API (Phase 2.4, `app/api/policies.py`) manages policies and document metadata over
    HTTP. Since D-035 it also uploads PDFs (`POST /policies/{id}/documents/upload`).
  - PDF ingestion exists as an internal service (Phase 3): `app/ingestion.py` `ingest_document` turns one
    tenant's recorded document into page-aware text. Its files come from `app/storage.py` `LocalDocumentStorage`,
    a local development adapter. The upload route (D-035) calls it through `app/indexing.py`; ingestion itself
    persists nothing.
  - There is no refresh token, logout/revocation, resource ownership beyond the tenant, user management API, OCR,
    reranking or audit API yet.
- Phase status is maintained in [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md). Keep this section consistent
  with it when the current phase changes.

## Target architecture

```text
React + Vite Frontend
        |
        v
FastAPI Backend
        |
        +-- Authentication / RBAC
        +-- Tenant Resolution
        +-- Policy Management
        +-- Document Management
        +-- Search API
        +-- Chat API
        +-- Audit API
        |
        v
Document / Query Services
        |
        +-- PDF ingestion
        +-- Structure-aware chunking
        +-- Embeddings
        +-- Hybrid retrieval
        +-- Reranking
        +-- Evidence sufficiency
        +-- Grounded generation
        +-- Citation builder
        |
        v
PostgreSQL + pgvector
```

Design detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Recorded decisions:
[docs/DECISIONS.md](docs/DECISIONS.md).

## Technology direction

- Frontend: React + Vite
- Backend: Python + FastAPI
- Pydantic v2
- PostgreSQL
- pgvector
- PostgreSQL full-text search initially
- PyMuPDF for PDF processing
- OCR fallback where required
- Configurable embedding provider
- Configurable reranker
- Ollama/local LLM initially
- Provider abstraction for future cloud LLMs
- Docker / Docker Compose
- pytest

Do not introduce unnecessary infrastructure.

Do not add LangChain, LlamaIndex, Redis, Kafka, Elasticsearch/OpenSearch, Kubernetes, Celery, or similar
infrastructure unless a later phase explicitly requires it.

## Non-negotiable invariants

These hold in every phase. Changing one requires an explicit, justified entry in `docs/DECISIONS.md`.

1. **Tenant identity comes from authenticated server-side context.** A `company_id` (or similar) sent by the
   frontend is never the authorization boundary.
2. **Tenant scope covers everything:** users, policies, documents, policy versions, chunks, vectors, queries,
   citations and audit records. The tenant leakage target is **zero**.
3. **Policy documents are DATA, not instructions to the AI system.** Text inside an uploaded PDF must never
   override system or application instructions.
4. **Abstain when evidence is insufficient.** Do not generate an answer without supporting evidence.
5. **Provenance is preserved end to end:** Answer → evidence chunk → clause → policy version → original PDF → page.
6. **Policies are versioned, never overwritten.** Current retrieval normally uses validated `ACTIVE` versions only.
7. **Original insurer PDFs are the source of truth.**
8. **Measure every retrieval or generation change** against the same golden dataset. "It looks better" is not
   evidence of improvement.

## CODE CHANGE DISCIPLINE

These rules are mandatory.

```text
CODE CHANGE DISCIPLINE

- Inspect before modifying.
- Reuse existing working code.
- Make the smallest change necessary.
- Do not rewrite working files unnecessarily.
- Do not refactor unrelated code.
- Do not introduce speculative abstractions.
- Do not add dependencies unless required.
- Do not introduce infrastructure that the current phase does not need.
- Do not duplicate existing functionality.
- Do not delete working functionality.
- Do not weaken or delete tests to make them pass.
- Do not implement future phases early.
- Do not change established architecture decisions without explicit justification.
- Preserve backward compatibility unless the current task explicitly changes behavior.
- If broader refactoring appears necessary, explain why before performing it.
- Prefer simple, explicit implementations over premature abstraction.
- Treat existing useful code as protected.
```

### Required task report

Every implementation task must report:

```text
Files modified:
Why each file was modified:
Files intentionally left unchanged:
Tests run:
Unrelated improvements noticed but NOT implemented:
```

## Documentation map

| File | Purpose |
|---|---|
| [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | Phase hierarchy, phase scope, and authoritative phase status |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design-level architecture: tenancy, versioning, retrieval, answering, provenance |
| [docs/EVALUATION.md](docs/EVALUATION.md) | Evaluation methodology, and the Phase -1 specification: metrics, golden dataset, gates, comparison rules, baseline |
| [docs/SECURITY.md](docs/SECURITY.md) | Security requirements and threat list |
| [docs/DATA_SPECIFICATION.md](docs/DATA_SPECIFICATION.md) | Planned development corpus and data layout |
| [docs/DECISIONS.md](docs/DECISIONS.md) | Architecture decision records and open questions |
| [docs/IMPLEMENTATION_LOG.md](docs/IMPLEMENTATION_LOG.md) | Dated log of what each task actually did |

### Keeping the documentation accurate

- After each task, add an entry to `docs/IMPLEMENTATION_LOG.md` and update statuses in `docs/PROJECT_PLAN.md`.
- When an architecture decision is added or changed, record it in `docs/DECISIONS.md` with its justification.
- Never describe something that is only documented as implemented. Mark a phase COMPLETE only after its checks
  are recorded.
- Edit the relevant section of a doc instead of rewriting the whole file.

## Commands

Backend: Python 3.12. This machine has `python3` but no `python` alias. Run these from `backend/`:

```bash
python3 -m venv .venv                       # once
.venv/bin/pip install -r requirements.txt   # once, and after dependency changes
.venv/bin/python -m pytest                  # tests (from the repo root: backend/.venv/bin/python -m pytest backend)
.venv/bin/uvicorn app.main:app --reload --no-access-log   # dev server: http://127.0.0.1:8000/health (--port if 8000 is taken)
.venv/bin/python -m app                     # same app, bound to the configured HOST/PORT (no reload)
```

Database. PostgreSQL must be running (`docker compose up -d postgres`). Load the Compose credentials into the shell
first: `set -a; . ../.env; set +a`.

```bash
.venv/bin/alembic upgrade head              # migrate the development database (or: docker compose exec backend alembic upgrade head)
.venv/bin/alembic current                   # show the applied revision
.venv/bin/python -m pytest -m database      # database tests, on a throwaway claimsrag_test_* database
```

Local `backend/.env` (D-035). When the backend runs on the host (`uvicorn` from `backend/`, without exporting the
root `.env`), it reads only `backend/.env` and the process environment. So that file must hold the database
settings as well as the keys. Without `POSTGRES_PASSWORD` every database request fails: startup logs a warning,
and requests get a 503 (this was the "sign-in 500").
1. Create it once from the example: `cp .env.example .env`.
2. Set `POSTGRES_PASSWORD` (and `POSTGRES_PORT` if needed) to the root `.env` values.
3. Add the keys below.

Access-token keys (D-017). Run once, from `backend/`, to add a development RSA key pair to `backend/.env` (git
ignored). The keys go straight into that file, and no `.pem` file is written. Never commit a key, never put one in
`.env.example`, and never reuse development keys in production.

```bash
PRIVATE_KEY="$(openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -quiet)"
printf 'JWT_PRIVATE_KEY="%s"\nJWT_PUBLIC_KEY="%s"\n' "$PRIVATE_KEY" "$(printf '%s\n' "$PRIVATE_KEY" | openssl pkey -pubout)" >> .env
unset PRIVATE_KEY
chmod 600 .env
```

### Configuration rules

- All configuration goes through `Settings` in `backend/app/config.py` (pydantic-settings). Don't read
  `os.environ` anywhere else.
- Variables:
  - `APP_NAME`, `APP_VERSION`, `ENVIRONMENT`, `DEBUG`, `LOG_LEVEL`
  - `HOST`, `PORT`
  - `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
  - `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`, `ACCESS_TOKEN_LIFETIME_MINUTES`
  - `OLLAMA_BASE_URL` (must name a local host), `LLM_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_OUTPUT_TOKENS`
  - `DOCUMENT_STORAGE_DIR` (server-side PDF storage, default `backend/.document_storage`), `MAX_UPLOAD_BYTES`
    (request body cap, default 25 MiB)
  - `DEMO_COMPANY_SLUG`, `DEMO_USER_EMAIL` (D-036: single-company demo access; both or neither; off by default)
- Precedence: explicit arguments > environment variables > `backend/.env` > defaults.
- `ENVIRONMENT` is `development` (the default), `test` or `production`. In `production`:
  - `POSTGRES_PASSWORD` is required and must not be the `.env.example` placeholder;
  - `JWT_PRIVATE_KEY` and `JWT_PUBLIC_KEY` are required;
  - `DEMO_COMPANY_SLUG` and `DEMO_USER_EMAIL` are refused.
- Secrets are `SecretStr` with no default. Never log them or return them from the API. Keep
  `hide_input_in_errors=True`: without it, validation errors print the password in plain text.
- `DEBUG` is deliberately **not** passed to FastAPI, because Starlette's debug mode returns stack traces to clients.
- Never branch on `ENVIRONMENT` or `DEBUG` outside `app/config.py` (log messages excepted). Authentication,
  authorization, tenancy and data access must behave the same in every environment, because the tests only run
  `ENVIRONMENT=test`.
- Tests run with `ENVIRONMENT=test`, set in `tests/conftest.py` before the app is imported. In that environment
  `backend/.env` is not read, so tests never depend on a developer's `.env` or shell.
- To add a setting:
  - add the field to `Settings`;
  - add its variable to `SETTINGS_ENV_VARS` in `tests/conftest.py`;
  - add a safe example value to the relevant `.env.example`.
- Never commit a real `.env`.

### Testing rules

- **Layout.**
  - `tests/unit/`: no app instance, no HTTP.
  - `tests/api/`: the app in-process through the `app` and `client` fixtures.
  - The shared fixtures (`settings`, `app`, `client`) live in `tests/conftest.py`. Add a feature's fixtures only once
    the feature exists.
- **The default `pytest -q` stays fast and needs no Docker, PostgreSQL or network.** An autouse guard fails any TCP
  connection, and any psycopg connection (libpq bypasses Python sockets), made by a test that isn't marked
  `database`.
- **Markers** (`--strict-markers`):
  - `database` needs PostgreSQL and is deselected by default. Run those tests with `pytest -m database`, after
    `docker compose up -d postgres`.
  - `security` marks security regressions in every layer. `pytest -m security` runs them all and needs PostgreSQL,
    like `-m database`. The database-free subset is `pytest -m "security and not database"`.
  - `embedding_model` needs the local model files in `backend/.model_cache`, and is deselected by default. Run
    those tests with `pytest -m embedding_model`. Download the files once (this is the only step that needs the
    network): `.venv/bin/python -c "from app.embeddings import FastEmbedProvider;
    FastEmbedProvider(allow_download=True)"`.
    - Embedding never sends policy text anywhere: the model runs in-process and loads offline.
    - Never log chunk text or vectors.
    - Embeddings are tenant-owned rows: reach them only through `TenantDatabase` and never update them.
    - `tests/database/test_retrieval_quality.py` carries both `database` and `embedding_model`, so
      `pytest -m database` also needs the model files.
- **Retrieval (Phase 6, D-029):**
  - Similarity SQL lives only in `TenantDatabase`, filters every joined tenant table on the tenant, and
    restricts to one embedding model (a structural test enforces this).
  - Filters only narrow the search (bound, `COALESCE`), never with `OR`.
  - The query is embedded alone, never with metadata.
  - Results never carry the company, vectors or storage references, and there is no answer field.
  - Any retrieval change must keep the regression set's baseline (invariant 8), or change it on purpose.
- **Answering (Phase 8, D-030):**
  - The marker `llm` needs the local Ollama server with `LLM_MODEL`, and is deselected by default. Run those
    tests with `pytest -m llm`. These tests may connect to loopback only.
  - Evidence reaches the model only as the `RetrievalResult` of server-side retrieval. Never accept evidence,
    a model, a prompt or a provider from a client.
  - Keep passages inside the tagged BEGIN/END EVIDENCE markers, chunk text exact, and the system prompt free of
    evidence.
  - Temperature, seed and context stay fixed in code, and the LLM URL stays local.
  - Never log questions, evidence or answers.
  - Prompt changes are measured on the real-model cases, as with retrieval.
  - Sufficiency, abstention and citations live in `app/grounding.py` (Phase 9), not in `app/answering.py`.
- **Cited answers (Phase 9, D-031):** `/chat` calls `answer_with_citations`, one model call returning a JSON verdict
  (`VERDICT_SCHEMA`) that `verify()` checks deterministically.
  - Any failed check abstains: the fixed sentence and no citations. Never return an unverified answer, and never
    return the abstention reason (log it).
  - Citations are built only from the `RetrievalHit` behind a label the server issued for this prompt. Never take
    a citation, page, chunk or document id from model text or from the client.
  - Retrieval scores never decide sufficiency.
  - Keep the number check (answer numbers ⊆ cited passages ∪ question), the citation-text check, and CONFLICT's
    two-passage minimum.
  - `json_schema` is server code only, never client input.
  - Verdict-rule changes are measured on the fixed real-model cases (`tests/unit/test_grounding_model.py`).
- **Warnings are errors** (`filterwarnings = error`). Fix the cause, or add a narrow, justified ignore in `pytest.ini`.
- **Database tests** live in `tests/database/` and each module sets `pytestmark = pytest.mark.database`.
  - Their fixtures create a `claimsrag_test_<random>` database per session, migrate it with Alembic, roll each test
    back, and drop that database afterwards. They never touch the development database `claimsrag`, and never run
    `docker compose down -v`.
  - They read `POSTGRES_HOST/PORT/USER/PASSWORD` from the shell and accept only a local server.
  - Missing configuration or an unavailable PostgreSQL makes them fail, never skip.
- **Moving or restructuring tests must keep their assertions.** Never weaken a test to make it pass.

### Logging & error-handling rules

- Use the standard `logging` module, with `logger = logging.getLogger(__name__)` in `app/` modules. Only
  `configure_logging()`, which `create_app()` calls, configures logging. `LOG_LEVEL` sets the level of the `app.*`
  loggers.
- Logs go to stdout/stderr only; Docker collects them. No log files, log volumes or external log services.
- **Never log** any of the following:
  - secrets and credentials;
  - `Authorization` or `Cookie` headers, tokens and API keys;
  - query strings and request bodies;
  - uploaded files and document/policy text.
- Request logs record only method, path (via `%r`), status and duration. `python -m app` turns off uvicorn's access
  log, which includes query strings.
- Every new secret setting must be added to the redaction list in `configure_logging()`.
- Unexpected exceptions:
  - are logged once, with the traceback, by the request-logging middleware;
  - give the client only `500 {"detail": "Internal server error"}`;
  - never put exception text in a response.
- Expected errors raise `HTTPException`, which keeps FastAPI's `{"detail": ...}` shape.
- `/health` is liveness only. It must not query the database.

### Database rules

- **Schema changes** are new Alembic revisions in SQL (D-014):
  - number them in order (`0002`, …), and never edit an applied migration;
  - give every constraint a name;
  - constrain allowed values with CHECK constraints;
  - include no seed data and no credentials.
- **No ORM.** SQLAlchemy is used only as Core, by Alembic.
- **Tenant key.** A company is the tenant (D-013). Every tenant-owned table carries `company_id` referencing
  `companies(id)`. The database enforces integrity; application checks are extra, not a replacement.
- **Same-company children (D-024).** A tenant-owned row that references another tenant-owned row carries the
  pair: a composite foreign key `(company_id, parent_id) → parent (company_id, id)`, with the parent declaring
  `UNIQUE (company_id, id)`. So the database itself refuses a child pointing at another company's parent. Example:
  `policy_documents_policy_fkey`.
- **Tenant-owned rows are never deleted silently.** Foreign keys are `ON DELETE RESTRICT`, which raises
  `RestrictViolation`, never `CASCADE`.
- **No file content in the database.** Store an opaque `storage_reference`, unique across all companies, plus
  metadata (size, SHA-256 checksum). Policy versions are new rows, never overwrites.
- **Email.** Emails are stored trimmed and lower-case; the database rejects anything else. Normalise before insert
  and lookup.
- **Password hashes.**
  - Store hashes only. Never log or return `password_hash`, which is a `SecretStr` in `app.models.User`.
  - PostgreSQL's error detail for a CHECK or NOT NULL violation includes the whole failing row, password hash
    included. Never log or return raw database error details for writes to `users`.
- Keep the `sqlalchemy.engine` logger at WARNING. At INFO it logs statements and their parameters.

### Authentication rules

- **Email:** `app.emails.normalize_email()` is the only email normalisation. Use it for every user lookup, creation
  and authentication. It does no provider-specific rewriting.
- **Passwords:** hash only with `app.passwords.hash_password()` (Argon2id, D-015) and check only with
  `verify_password()`. Never compare hashes yourself, and never change the pinned parameters casually.
- **Credentials:** `authenticate(conn, company_slug, email, password)` (D-016) returns `AuthenticatedUser`, which
  holds no credentials. Every failure raises the same `AuthenticationError("Invalid credentials")`. Never expose why
  a login failed.
- **Never log** passwords, password hashes, or the email address used in a login attempt. Authentication logs the
  outcome, the reason and the user id only.
- **Access tokens (D-017):**
  - Issue only with `app.tokens.issue_access_token(AuthenticatedUser, settings)`. Never build claims from client
    input.
  - Verify only with `decode_access_token(token, settings)`. It returns `TokenIdentity` (`user_id`, `company_id`,
    `role`).
  - The algorithm is the code constant `ALGORITHM = "RS256"`. Never take it from the token header, and never make it
    configurable.
  - Every invalid token raises the same `AccessTokenError`.
  - Never log tokens, claims or key material. `JWT_PRIVATE_KEY` is a secret and is in the log redaction list.
- **Sign-in (D-032):** `POST /auth/login` (`app/api/auth.py`) issues tokens for credentials. It is one of the three
  public writes (`PUBLIC_WRITES`). The frontend no longer uses it (D-036).
  - Its only dependency is `database_connection`. Never add identity, tenant or permission dependencies, or a
    tenant/role field.
  - Every authentication failure is the same 401 "Invalid credentials".
  - Its fields default to empty strings, so a missing field reaches the route. It gets a fixed 422 naming the
    field (D-035), never FastAPI's 422, which echoes the body and the password.
  - Its response is `{access_token, token_type, expires_in, user: {full_name, email, role}}`: never a company id,
    status or hash.
- **Company registration (D-035):** `POST /auth/register-company` is the second public write. The frontend no
  longer uses it (D-036).
  - Its only dependency is `database_transaction`.
  - `app/registration.register_company` validates every field (`password_problem`, `normalize_email`), then runs
    **one** statement creating the company and its first admin, with the role literal `'COMPANY_ADMIN'`.
  - Never add a role, company-id or status field, never split the statement, never return ids or the hash, and
    keep the fixed-shape 422 (never echo input).
  - A taken code is a 409. The structural tests pin the statement.
- **Demo session (D-036):** `POST /auth/demo-session` is the third public write. The frontend calls it on start.
  - Its only dependency is `database_connection`. It has **no inputs** (no body, path, query, header or cookie),
    and the structural tests pin that: never add one.
  - It is a 404 (FastAPI's own body) unless both demo settings are set; `Settings` refuses them in production.
  - `app.auth.demo_account` looks up the configured user within the configured company by slug, never selecting
    `password_hash`. Every unavailable case (missing, other-company or inactive user, inactive company, missing
    keys) is the same 503; log the reason and user id only.
  - The token comes from `issue_access_token`, as for sign-in. Never let the request influence the company, user
    or role.
- **Out of scope until later tasks:**
  - refresh tokens, logout and revocation;
  - rate limiting, lockout and MFA.

  Don't add them early.

### Authorization rules

- **Permissions** are granted only in `app.rbac.ROLE_PERMISSIONS` (D-018). Never compare roles anywhere else (no
  `if role == "COMPANY_ADMIN"`), and don't add roles: V1 has only `COMPANY_ADMIN` and `STAFF`.
- **Checks:**
  - In services: `require_permission(identity, Permission.X)`.
  - On routes: `Depends(permission_required(Permission.X))` from `app.api.authorization`.
  - Always use `Permission` members, never strings, and keep the `resource:action` naming.
- **The identity is the `TokenIdentity` from the verified token.** Never take a role, permission or `company_id`
  from the request body, query string or headers.
- **Default deny:** an unknown role, an unknown permission or a non-`TokenIdentity` gets nothing. Never add a
  fallback role or an admin wildcard.
- **401 vs 403:** a missing or invalid token is 401; a valid identity without the permission is 403
  (`AuthorizationError`). Keep the response bodies generic.
- **RBAC is not tenant isolation.** A permission says what a role may do, not which company's data it may touch.
  See "Tenant rules".
- **Test routes** for HTTP authorization belong in tests only, added to a test app instance, never to
  `create_app()`.
- **Every API route declares its permission (D-021).**
  - Use `Depends(permission_required(Permission.X))` on the route, a parameter, its router or `include_router`.
  - `create_app()` refuses to start if a route has none, except GET/HEAD on `app.api.authorization.PUBLIC_PATHS`
    (`/`, `/health`) and FastAPI's documentation pages (D-022). Authentication-only or tenant-only routes count as
    missing one.
  - Plain Starlette routes, websockets and mounted apps are refused too: they cannot carry `permission_required`.
  - Making a route public is an explicit, reviewed change to `PUBLIC_PATHS` (reads) or `PUBLIC_WRITES`, whose
    entries are `POST /auth/login` (D-032), `POST /auth/register-company` (D-035) and `POST /auth/demo-session`
    (D-036). The structural tests pin all three.
  - Register routes inside `create_app()`, before the guard runs. Never add a route to the module-level `app`
    afterwards.
- **Declare the permission before any other work (D-023).**
  - Put it in `dependencies=[...]` on the route or its router, or make it the first dependency parameter.
  - Only `current_identity` and `current_tenant` may be resolved before it.
  - Why: FastAPI resolves dependencies in declaration order, so a dependency declared earlier (one that loads or
    changes data, say) would run even for a request that is then denied.
- **Structural security tests pin the boundary (D-022)**
  (`tests/unit/test_security_structure.py`, `tests/api/test_route_security.py`):
  - one token-verification site, and one construction site each for `TokenIdentity` and `TenantIdentity`;
  - no request-data reads, and no client tenant/role parameters;
  - scoped SQL, and no role comparisons.
  - Added in Phase 1.9 (D-023):
    - no identity copied with changed fields or built unvalidated (`model_copy(update=...)`, `model_construct`);
    - no behaviour outside `app/config.py` depends on `ENVIRONMENT` or `DEBUG`;
    - only the login lookup reads `password_hash`;
    - nothing but authentication and tenant resolution runs before a route's permission check;
    - the served `app.main:app` is checked like `create_app()`, and its public paths are served only by the two
      dependency-free public endpoints.

  When adding a tenant-owned table, add it to `TENANT_OWNED_TABLES` there. If one of these tests fails, fix the
  code, not the test.
- **Service-level checks** (`require_permission()` inside a route or service) raise `AuthorizationError`, which
  `app/errors.py` turns into the generic 403. Don't catch it and continue.

### Tenant rules

- **The tenant is the verified token's company id (D-019), and nothing else.**
  - In routes: `tenant: Annotated[TenantIdentity, Depends(current_tenant)]` (`app.api.tenancy`).
  - In services: `resolve_tenant(identity)` (`app.tenancy`) on a `TokenIdentity`.
  - Never build a `TenantIdentity` from request data.
- **Never take a tenant from the client.** A `company_id` in the path, query string, body, headers or cookies is
  never the tenant, is not compared against it, and is not used as a fallback.
- **One authentication path.** Tenant code gets the identity from `current_identity`. Never read the
  `Authorization` header or decode a token anywhere else.
- **Fail closed.** No default, first, nil, global or "system" tenant, and no tenant switching or super-admin tenant.
- **Keep it separate.** Tenant resolution doesn't check permissions (RBAC) or resource ownership, and doesn't query
  the database.

### Tenant data-access rules

- **Every query on tenant-owned data goes through `app.tenant_db.TenantDatabase(conn, tenant)` (D-020).**
  - `tenant` is the `TenantIdentity` from `current_tenant` / `resolve_tenant`.
  - Never query a tenant-owned table on a bare connection. The three exceptions run before any tenant exists:
    `authenticate()` (the sign-in lookup), `demo_account()` (D-036: the configured demo user's lookup, by slug) and
    `register_company()` (D-035: the statement that creates the tenant and its first admin).
- **Add queries as `TenantDatabase` methods.**
  - Use literal SQL that filters `company_id = %(company_id)s`, and pass rows through `_own()`.
  - The tenant is bound by `_fetch()`. Never add a `company_id` parameter to a method, and never add a raw
    `execute`.
- **Writes are tenant-scoped in the SQL itself (Phase 2.2).**
  - **INSERT:** `company_id` is the first column and `%(company_id)s`, bound from the tenant, is its value.
  - **UPDATE/DELETE:** filter `WHERE company_id = %(company_id)s AND id = ...` in the same statement. Never fetch,
    check in Python, then write unscoped.
  - **Never assign `company_id`** in a SET clause: a row never changes tenant. Never take a new primary key from
    the caller.
  - **Another tenant's id** behaves exactly like a missing one (`None` / `False`).
  - **Database errors** (`CheckViolation`, `UniqueViolation`, `RestrictViolation`) propagate unchanged; don't
    swallow them or turn "still has documents" into "not found".
  - **No commits:** methods never commit; the caller owns the transaction.
  - **No RBAC here:** a future route that calls a write must declare `policy:manage`.
- **Document writes follow the same rules, plus (Phase 2.3):**
  - **Same-company parent:** a document INSERT puts `policy_id` right beside the tenant's `company_id`
    (`VALUES (%(company_id)s, %(policy_id)s, ...)`). The composite foreign key then refuses another tenant's policy
    exactly like a missing one (`ForeignKeyViolation` on `policy_documents_policy_fkey`). Never look the policy up
    first and copy its company.
  - **A version is fixed once recorded (D-025):** only `original_filename` (`update_document`) and `status`
    (`set_document_status`, `UPLOADED`/`INACTIVE` only) change in place. A new file, key or version is a new row
    (`create_document`). Never SET `company_id`, `id`, `policy_id`, `document_key`, `version` or the file fields.
  - **Versions are given, not computed:** no automatic numbering, superseding or current-version selection here
    (Phase 11). A delete removes one version only, never its siblings or policy.
  - **Storage references come from the server.** Never accept one from a client: they are unique across all
    companies, so a client-chosen value would reveal whether another tenant's file exists.
- **The structural SQL rule (Phase 2.3)** also requires that an UPDATE or DELETE's whole WHERE clause be
  `company_id = %(company_id)s AND id = %(<x>_id)s`, that no statement use `OR` in WHERE or start with anything but
  SELECT/INSERT/UPDATE/DELETE, and that no SET assign `company_id`, `id` or `policy_id`.
- **Never select `password_hash`** in tenant reads.
- **No role bypass.** `TenantDatabase` has no role input. No super-admin, cross-company or "all tenants" access.
- **The database layer stays unaware of HTTP, tokens and RBAC.** No FastAPI/Starlette or JWT imports, and no
  headers, cookies or roles.
- **Application-level enforcement only.** Don't add PostgreSQL row-level security, `SET LOCAL app.company_id` or
  per-tenant roles without a decision.

### Policy & document API rules (Phase 2.4, D-026)

- **Routes** live in `app/api/policies.py`, registered by `create_app()`.
  - `GET` needs `policy:read`; `POST`, `PATCH`, `PUT` and `DELETE` need `policy:manage`.
  - The permission goes in `dependencies=[READ]` or `[MANAGE]`, first.
- **Database access in a route** comes only from the `TenantDb` parameter (`app/api/database.py`):
  - one connection and one transaction per request;
  - committed after the route returns but **before** the response is sent (`scope="function"`), and rolled back
    if it raises, `HTTPException` included;
  - never open a connection, commit or build a `TenantDatabase` in a route.
- **Request bodies** subclass `_Request` (`extra="forbid"`, `StrictStr`, strict integers).
  - Never add a `company_id`, role, permission, `id`, `policy_id` or `storage_reference` field. The tenant comes
    from the token, ids from the path, and the storage reference from the server (`_new_storage_reference()`).
- **Responses** are the explicit `PolicyResponse` / `DocumentResponse` models. They never include `company_id` or
  `storage_reference`, and are never internal models returned directly.
- **Errors:**
  - Another tenant's policy or document is the same 404 (same body) as a missing one. That includes creating a
    document under a foreign policy, which the database refuses with `ForeignKeyViolation`.
  - Database refusals go through `_refusals()`, which maps them to fixed messages: duplicates and "policy still has
    documents" are 409; CHECK and data errors are 422 naming only the field. Anything unmapped is the generic 500.
  - Never put exception text, SQL, constraint or table names in a response.
- **Document PATCH** changes only `original_filename`, and `PUT /documents/{id}/status` only the status (D-025).
- **Upload (D-035):** `POST /policies/{policy_id}/documents/upload`, behind `policy:manage` (declared first).
  - The PDF is the raw `application/pdf` body; `filename`, `document_key` and `version` are query parameters.
  - Indexing is `app.indexing.index_document` only. The operator tooling uses it too: never duplicate the
    store → record → ingest → chunk → embed pipeline.
  - The file is validated (`extract_pages`) before anything is stored. It is stored only under
    `new_storage_reference()` in `DOCUMENT_STORAGE_DIR`, never under a client name or path.
  - The client's file name goes through `safe_filename` and is display metadata only.
  - Refusals use fixed codes (`unreadable_pdf`, `no_text`, `empty_file`), never parser text.
  - `RequestSizeLimit` caps every body at `MAX_UPLOAD_BYTES` (413).
- **Tests** replace only `database_connection` with the test's rolled-back connection, so the real transaction
  dependency still runs.

### PDF ingestion rules (Phase 3)

- **Entry point:** `ingest_document(db: TenantDatabase, storage: LocalDocumentStorage, document_id)`. It returns a
  `DocumentExtractionResult`: `document_id`, `policy_id`, `document_key`, `version`, `original_filename`,
  `checksum_sha256`, `page_count`, and `pages` of `ExtractedPage(page_number, text)`.
  - The document comes only from the tenant's record, so another tenant's id is `NOT_FOUND`, like an unknown one.
  - Never add a company, path or storage-reference parameter. The file is always the record's own reference.
- **Storage:** `LocalDocumentStorage(root)` accepts only server-format references (32–128 URL-safe
  characters). So a reference is never a path, links and non-files are refused, files are write-once, and errors
  never include a path. The root comes from server code, never from a client.
- **Integrity:** the bytes must match the record's `size_bytes` and `checksum_sha256`. These PDFs are refused as
  `UNREADABLE_PDF`:
  - non-PDF content, which MuPDF would otherwise open by sniffing it;
  - encrypted PDFs;
  - repaired PDFs (`is_repaired`: damaged, with text missing).
- **Text is evidence:** `normalize_page_text` changes only line endings, control characters, runs of spaces and
  blank lines, and applies NFC. Never dehyphenate, case-fold, apply NFKC, summarise or reorder.
- **Page boundaries:** pages keep their PDF order and 1-based numbers, and an empty page stays in place. Text
  blocks are joined by one blank line, which keeps paragraph boundaries for Phase 4.
- **No text on any page** (a scan) is `NO_TEXT`, never an empty success. OCR is a later enhancement.
  `INACTIVE` documents are not ingested.
- **Read-only:** ingestion writes nothing and does not change the document's status. There is no processing
  status yet, because statuses are schema changes (D-024).
- **Logs:** document id, outcome and page counts only. Never text, paths or storage references.

### Operator tooling (D-032)

`backend/scripts/workspace.py` prepares a workspace on the server. It is not part of the served application, and
`app/` never imports it. Run it from `backend/` with the database settings loaded (`set -a; . ../.env; set +a;
export POSTGRES_PORT=$POSTGRES_HOST_PORT`):

```bash
.venv/bin/python -m scripts.workspace create-company --name "Demo Health Insurance (sample)" --slug demo-health
.venv/bin/python -m scripts.workspace create-user --company demo-health --email admin@demo-health.example \
    --name "Asha Menon" --role COMPANY_ADMIN   # password prompted, or from CLAIMSRAG_USER_PASSWORD
.venv/bin/python -m scripts.workspace sample-pdf --out sample.pdf                  # synthetic sample wording
.venv/bin/python -m scripts.workspace index-document --company demo-health --policy DH-100 --pdf sample.pdf
```

- **Passwords:** never pass one as an argument; there is no `--password` option.
- **`index-document`:** needs the policy to exist (an admin creates it in the frontend), and the embedding model
  files.
- **Storage:** files go to `DOCUMENT_STORAGE_DIR` (default `backend/.document_storage/`, git-ignored), the same
  directory as uploads.

### Frontend (D-033, D-036)

`frontend/` is React + Vite + TypeScript. Run these from `frontend/` (Node 20+):

```bash
npm install                                         # once, and after dependency changes
CLAIMSRAG_API_URL=http://127.0.0.1:8002 npm run dev  # http://127.0.0.1:5173, forwarding /api to the backend
npm test                                            # Vitest + Testing Library (no network, no backend)
npm run typecheck                                   # app (tsconfig.json) and Node-side files (tsconfig.node.json)
npm run build                                       # type-check, then build into dist/
npm run e2e                                         # Playwright in the installed Chrome, against the real stack
```

- **Running the backend for the frontend:** run the backend on the host (`uvicorn app.main:app --port 8002`, from
  `backend/`, with the JWT keys and database settings in `backend/.env`) and point `CLAIMSRAG_API_URL` at it.
- **Demo access (D-036):** the frontend opens directly as the user in `DEMO_COMPANY_SLUG` / `DEMO_USER_EMAIL`
  (`backend/.env`; locally `demo-health` and its admin). That user must exist and be active (operator tooling). Without
  them the app shows "Demo access isn't configured", and the API's other routes still need a token.
- **`npm run e2e` needs:**
  - the backend (with demo access on, for an active COMPANY_ADMIN), PostgreSQL, Ollama and `npm run dev` running;
    `E2E_BASE_URL` if the dev server is not on 5173;
  - `E2E_SAMPLE_PDF`: `python -m scripts.workspace sample-pdf --out <path>`.
  - Each run adds a test policy and withdraws its document. Point the backend at a separate company
    (`DEMO_COMPANY_SLUG=... DEMO_USER_EMAIL=... uvicorn ...`, overriding `backend/.env`) to keep the demo workspace
    clean.
- **Frontend rules:**
  - All API calls go through `src/api/client.ts` and `src/api/endpoints.ts`. Uploads use `uploadFile` there
    (XMLHttpRequest, for real upload progress), with the same token and error mapping.
  - Never send a company, tenant, role or permission, and never show server error text. Map errors to the fixed
    messages in `client.ts`.
  - The role (`src/auth/permissions.ts`) only hides controls. Every action must still handle the backend's 403.
  - Render answers, citations and evidence as the API returns them.
    - A `[k]` becomes a link only when citation k exists.
    - `ABSTAINED` and `CONFLICT` must never look like an answer.
    - Search results are passages, not answers.
  - Never render or store the token. It lives in memory (`src/auth/AuthContext.tsx` `SessionProvider`) and in the
    bearer header only. The provider renews it before expiry and once after a 401 (`client.ts` retries the request
    once).
  - There is no sign-in, registration or sign-out screen. `/login` and `/register` redirect to `/`. Don't add a
    login screen back without a decision.
  - `VITE_*` variables are compiled into the bundle and are public: never put a secret in one.
  - Colours, type and spacing come from the tokens in `src/styles/global.css`. Status is never shown by colour
    alone.

Docker Compose (backend + PostgreSQL 18 with pgvector). Run these from the repo root:

```bash
cp .env.example .env    # once; placeholder dev values. Change BACKEND_HOST_PORT / POSTGRES_HOST_PORT if a port is taken
docker compose up -d    # add --build after backend changes (the container has no hot reload)
docker compose ps       # both services should be "healthy"
docker compose down     # stop; data stays in the claimsrag_postgres_data volume (`down -v` deletes it)
```

- Ports are published on `127.0.0.1` only.
- Inside the Compose network the backend reaches PostgreSQL at `postgres:5432`.
- `docker/postgres/init/01-enable-pgvector.sql` runs only when the volume is first created.
- On this machine, port 8000 is taken by another project's container, so the local `.env` sets
  `BACKEND_HOST_PORT=8001`.

## Related material outside this repository

`../PROJECT CODEBASE/` holds an earlier ClaimsRAG prototype. That prototype audits claim bills and answers policy
questions over synthetic JSON clauses. Its tenancy is based on headers, and it uses in-memory TF-IDF/fastembed
retrieval, pdfplumber and Supabase. It is **not** part of this repository, and its scope and architecture differ from
this plan. Treat it as read-only. Do not modify it, and do not copy code or design from it unless the user approves.
This is an open question in `docs/DECISIONS.md`.
