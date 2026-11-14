# ClaimsRAG — Local Development Run Guide

This guide explains how to start and run the ClaimsRAG project locally.

## 1. System Architecture

ClaimsRAG runs as a local application with four main components:

```text
┌─────────────────────┐
│   React Frontend    │
│      Vite :5173     │
└──────────┬──────────┘
           │ /api
           ▼
┌─────────────────────┐
│   FastAPI Backend   │
│       :8000         │
└────────┬─────┬──────┘
         │     │
         ▼     ▼
   PostgreSQL     Ollama
   + pgvector     qwen2.5:3b
```

### Components

| Component | Purpose | Typical address |
|---|---|---|
| React + Vite | ClaimsRAG user interface | `http://localhost:5173` |
| FastAPI | API and RAG orchestration | `http://localhost:8000` |
| PostgreSQL + pgvector | Application data, chunks and embeddings | Docker |
| Ollama | Local LLM for grounded answers | `http://localhost:11434` |

The frontend uses `/api` and the Vite development server forwards these requests to FastAPI.

---

# 2. Prerequisites

Make sure these are installed:

- Docker
- Docker Compose
- Python 3.12
- Node.js and npm
- Ollama
- Git

The project should already contain the Python virtual environment and required frontend/backend dependencies.

---

# 3. Start PostgreSQL

Open a terminal in the project root.

```bash
docker compose up -d postgres
```

Check that PostgreSQL is running:

```bash
docker compose ps
```

You should see the PostgreSQL service running.

The project uses PostgreSQL with the `pgvector` extension.

---

# 4. Start Ollama

Open another terminal.

Start Ollama if it is not already running:

```bash
ollama serve
```

Keep this terminal running.

In another terminal, check the installed models:

```bash
ollama list
```

ClaimsRAG currently uses:

```text
qwen2.5:3b
```

If the model is not installed, install it:

```bash
ollama pull qwen2.5:3b
```

ClaimsRAG uses Ollama locally for the grounded answer generation.

---

# 5. Start the Backend

Open another terminal.

```bash
cd backend
```

Activate the Python virtual environment:

```bash
source .venv/bin/activate
```

Start FastAPI:

```bash
uvicorn app.main:app --reload --port 8000
```

The backend should be available at:

```text
http://localhost:8000
```

Keep this terminal running.

---

# 6. Start the Frontend

Open another terminal.

```bash
cd frontend
```

Start the Vite development server:

```bash
npm run dev
```

Open the URL shown by Vite.

Normally it is:

```text
http://localhost:5173
```

---

# 7. Check the Application

Open the ClaimsRAG frontend in a browser.

The normal development flow is:

```text
Open ClaimsRAG
      ↓
Sign in
      ↓
Overview
      ↓
Create / select a policy
      ↓
Upload a policy PDF
      ↓
Document processing
      ↓
Document becomes available
      ↓
Search policy evidence
      ↓
Ask a question
      ↓
Receive grounded answer
      ↓
View citations and exact evidence
```

---

# 8. Policy Upload

The intended user flow is:

```text
Company Admin
      ↓
Create Policy
      ↓
Upload Policy PDF
      ↓
Backend receives PDF
      ↓
Store PDF
      ↓
Extract text
      ↓
Create chunks
      ↓
Generate embeddings
      ↓
Store embeddings in PostgreSQL
      ↓
Policy becomes searchable
```

The frontend must not perform PDF extraction, chunking or embedding itself.

Those operations belong to the backend services.

The existing processing pipeline is:

```text
PDF
 ↓
Phase 3 — Ingestion
 ↓
Phase 4 — Chunking
 ↓
Phase 5 — Embeddings
 ↓
PostgreSQL + pgvector
 ↓
Phase 6 — Hybrid Retrieval
 ↓
Phase 8/9 — Grounded Answer + Citations
```

---

# 9. Search

After a policy has been processed, open the Search page.

Search is performed by the backend.

The retrieval pipeline is:

```text
Search question
      ↓
Query embedding
      ↓
Vector search
      +
Keyword search
      ↓
Reciprocal Rank Fusion
      ↓
Top evidence chunks
```

The results show policy/document/page/section provenance.

---

# 10. Ask a Question

Open the Chat / Ask a Question page.

Enter a question that should be answered by the uploaded policy.

The current pipeline is:

```text
Question
   ↓
Tenant-scoped retrieval
   ↓
Evidence
   ↓
Local Ollama model
   ↓
Verified grounding verdict
   ↓
Answer / Conflict / Abstention
   ↓
Citations
   ↓
Exact evidence
```

For an answerable question, the UI should show the answer and its supporting citations.

For a question that cannot be supported by the available evidence, ClaimsRAG should abstain rather than invent an answer.

---

# 11. Demo Example

The existing local E2E verification used a sample policy called:

```text
DH-100
```

The sample policy contained:

```text
4 pages
5 chunks
```

A tested question was:

```text
What is the waiting period for maternity expenses?
```

The expected demonstrated answer was:

```text
9 months
```

The evidence was from:

```text
DH-100
Version 1
Page 3
Section 3
```

A no-claim-bonus question was also tested and correctly resulted in abstention.

---

# 12. Useful Commands

## Backend tests

From the `backend` directory:

```bash
pytest
```

Security tests:

```bash
pytest -m security
```

Database tests:

```bash
pytest -m database
```

Compile check:

```bash
python -m compileall -W error app tests
```

Dependency check:

```bash
pip check
```

---

## Frontend tests

From the `frontend` directory:

```bash
npm test
```

Type checking:

```bash
npm run typecheck
```

Build:

```bash
npm run build
```

End-to-end tests:

```bash
npm run e2e
```

---

# 13. Check Service Status

## Docker

```bash
docker compose ps
```

## Ollama

```bash
ollama list
```

## Backend

Open:

```text
http://localhost:8000
```

## Frontend

Open the Vite URL, normally:

```text
http://localhost:5173
```

---

# 14. Stopping the Application

Stop the frontend with:

```text
Ctrl+C
```

Stop the FastAPI backend with:

```text
Ctrl+C
```

Stop Ollama with:

```text
Ctrl+C
```

Stop the PostgreSQL container:

```bash
docker compose down
```

Do not use commands that remove the PostgreSQL volume unless you intentionally want to delete the development database.

---

# 15. Important Security Notes

Do not commit:

```text
backend/.env
```

or private development keys.

The project uses tenant-scoped authentication and authorization.

The browser must not be trusted for:

- company ID
- tenant ID
- role
- permission

The backend determines the authenticated tenant from the verified token.

Do not put real insurance customer data into the local development environment unless the data is authorized for development use.

---

# 16. Current Limitations

The current project is a development/local system.

Known limitations include:

- PDF upload is being integrated through the frontend/backend workflow.
- No production deployment configuration is complete.
- Ollama is currently local.
- The embedding model is local.
- Authentication has no refresh-token system.
- Authentication has no rate limiting or account lockout.
- User management UI is not available because the corresponding API is not implemented.
- Phase 7 reranking remains deferred.
- Advanced evaluation work remains deferred.
- Policy freshness/versioning remains deferred.
- Multi-document reasoning remains deferred.
- Policy Schedule remains deferred.
- Production hardening remains deferred.

These limitations are part of the current project scope and should not be treated as implemented features.

---

# 17. Recommended Development Startup Order

When starting work from a fresh terminal session, use this order:

### Terminal 1 — PostgreSQL

```bash
docker compose up -d postgres
```

### Terminal 2 — Ollama

```bash
ollama serve
```

### Terminal 3 — Backend

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000
```

### Terminal 4 — Frontend

```bash
cd frontend
npm run dev
```

Then open the frontend in the browser.

---

# 18. Full ClaimsRAG Development Flow

```text
                    CLAIMSRAG
                       │
                       ▼
              ┌────────────────┐
              │ React Frontend │
              │    Vite        │
              └───────┬────────┘
                      │ /api
                      ▼
              ┌────────────────┐
              │ FastAPI        │
              │ Authentication │
              │ RBAC           │
              │ Tenant Control │
              └───────┬────────┘
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
      PostgreSQL   PDF Pipeline   Ollama
      + pgvector       │           │
                       ▼           │
                Extract → Chunk    │
                       ↓            │
                   Embeddings       │
                       ↓            │
                  pgvector          │
                       │            │
                       └─────┬──────┘
                             ▼
                       Retrieval
                             ↓
                       Grounding
                             ↓
                    Answer + Citation
                             ↓
                       Evidence UI
```

This is the normal way to run the ClaimsRAG development environment.
