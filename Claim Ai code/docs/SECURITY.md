# ClaimsRAG — Security Requirements

> **Status:** requirements and threat list. None of the threat mitigations in §2 is fully implemented yet. Phase
> 1.2 added the authentication baseline (§3), Phase 1.3 the access tokens (§4), Phase 1.4 RBAC (§5), Phase 1.5
> tenant resolution (§6), Phase 1.6 tenant-aware database access (§7), Phase 1.7 API authorization (§8) and
> Phase 1.8 cross-tenant security verification (§9). Phase 1.9's final review (§10) closed Phase 1. Phase 2.1
> extended tenant ownership to policies and policy documents (§11), Phase 2.2 added tenant-scoped policy writes
> (§12), Phase 2.3 tenant-scoped policy-document writes (§13), and Phase 2.4 the policy and document API
> (§14), reviewed in Phase 2.5. Phase 9 added the controls for citation manipulation, hallucinated coverage and
> out-of-scope questions (T7, T8, T10) to the chat answers (§15). Frontend & Final Integration added sign-in and
> the frontend (§16). Broad hardening is Phase 14 (see
> [PROJECT_PLAN.md](PROJECT_PLAN.md)).
>
> Phase 0 added only baseline safeguards (see "Configuration rules" and "Logging & error-handling rules" in
> [CLAUDE.md](../CLAUDE.md)):
> - generic 500 responses;
> - secrets hidden from configuration errors and redacted from logs;
> - a request log limited to method, path, status and duration;
> - ports published on 127.0.0.1 only;
> - a backend container that runs as a non-root user.

## 1. Core security principles

```text
Tenant leakage target: ZERO
```

> **Policy documents are DATA, not instructions to the AI system.**

Text inside an uploaded PDF must never override system or application instructions. This holds whether the text
is visible, hidden, in metadata, or produced by OCR.

- Tenant identity comes from authenticated server-side context. A `company_id` supplied by the frontend is never the
  authorization boundary.
- Tenant scope applies to users, policies, documents, policy versions, chunks, vectors, queries, citations and
  audit records (see [ARCHITECTURE.md](ARCHITECTURE.md) §2).
- When evidence is insufficient, the system abstains and does not guess.

## 2. Threats

The mitigation directions below are design intent. Each phase sets the specific controls.

| # | Threat | What could go wrong | Mitigation direction | Phase |
|---|---|---|---|---|
| T1 | Cross-tenant data leakage | One company's policies, chunks, vectors or citations reach another company. | Tenant filter on every query path before ranking. Tenant context on every stored record. Isolation tests with a zero-leakage target. | 1, 6, 10 |
| T2 | Unauthorized document access | A user reads or downloads a document outside their tenant or role. | Server-side authorization on every document and citation endpoint. | 1, 2 |
| T3 | Prompt injection in PDFs | Text in a PDF tells the model to ignore its rules, reveal data or change behaviour. | Treat document text strictly as data and keep it separate from instructions. Test with an injection corpus. | 8, 9, 14 |
| T4 | Document poisoning | A planted or altered document produces misleading answers. | Restrict uploads by role. Track original PDFs and versions. Validate before activation. Test with poisoning cases. | 2, 11, 14 |
| T5 | Stale policy retrieval | An answer cites a superseded version when an active version applies. | Retrieve only validated `ACTIVE` versions by default. Test freshness. | 11 |
| T6 | Policy-version mixing | One answer combines evidence from different versions of the same policy without saying so. | Record the version on every chunk and citation. Enforce version consistency in the evidence set. | 4, 9, 11 |
| T7 | Citation manipulation | A citation points to evidence that was not retrieved or does not support the claim. | Validate citations against the actual evidence set before returning the answer. | 9 |
| T8 | Hallucinated coverage | The system says something is covered (or excluded) without supporting evidence. | Check evidence sufficiency and abstain when it is insufficient. Measure faithfulness. | 9, 10 |
| T9 | Numerical/table errors | Amounts, limits, percentages or table values are changed in the answer. | Preserve table structure when chunking. Measure numerical fidelity and table QA accuracy. | 4, 10 |
| T10 | Out-of-scope questions | The system answers questions the policies don't cover, or questions unrelated to policies. | Abstain. Measure abstention accuracy and the false-answer rate. | 9, 10 |
| T11 | Malicious or malformed PDFs | Crafted files crash or exhaust the parser, or carry hostile content. | Validate file type, enforce size/page limits, and handle parse failures safely. | 2, 3, 14 |
| T12 | Privilege escalation | A `STAFF` user performs `COMPANY_ADMIN` actions, or a user gains access to another tenant. | Server-side RBAC checks on every protected action. Tests for each role and endpoint. | 1 |
| T13 | Frontend tenant manipulation | A user edits request parameters (e.g. `company_id`) to reach another tenant. | Resolve the tenant from the authenticated context only. Reject mismatches. | 1 |

## 3. Authentication baseline (Phase 1.2)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.2 and D-015/D-016 in [DECISIONS.md](DECISIONS.md)):

- **Password hashing:** Argon2id with pinned RFC 9106 low-memory parameters and a random salt per hash. Only hashes
  are stored, never plaintext.
- **Password verification:** through the library's verification only, with no manual comparison. A malformed or
  unsupported stored hash fails safely as a normal failed login. A warning is logged without the hash.
- **Generic authentication failure:** unknown company, unknown email, wrong password, inactive user and inactive
  company all raise the same `AuthenticationError("Invalid credentials")`. An unknown account still costs one Argon2id
  verification, against a dummy hash, so it does not answer measurably faster than a real one.
- **Inactive-user rejection:** inactive users, and users of inactive companies, cannot authenticate even with the
  correct password. Status is checked only after the password, so it is never revealed without it.
- **Safe identity representation:** `AuthenticatedUser` holds identity only, with no password or hash.
  `app.models.User` masks `password_hash`.
- **No credentials in logs:** authentication logs the outcome and the user id only. They never contain passwords,
  password hashes or the email address.

**Not implemented:** brute-force protection, rate limiting, account lockout, MFA, password policy, password reset and
email verification. Access tokens are §4.

## 4. Access tokens (Phase 1.3)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.3 and D-017 in [DECISIONS.md](DECISIONS.md)):

- **Signing algorithm:** RS256. It is fixed in code (`app/tokens.py`), is not an environment setting, and is never
  read from a token.
- **Algorithm allow-list:** verification accepts RS256 only. Tests show that each of these is rejected:
  - `alg=none` (any case);
  - HS256 keyed with the public key (algorithm confusion);
  - RS384/RS512/PS256/PS384/PS512 tokens genuinely signed with the configured key;
  - an elliptic-curve token;
  - a header relabelled to another algorithm;
  - a key embedded in the header (`jwk`).
- **Key configuration:**
  - `JWT_PRIVATE_KEY` (a `SecretStr`, redacted from logs) and `JWT_PUBLIC_KEY` are PEM text from the environment or
    `backend/.env`.
  - They are validated when the settings load: both or neither, an unencrypted RSA key of at least 2048 bits, a
    matching pair. The error messages never include key material.
  - Production refuses to start without them.
  - No key is in the code, the repository or `.env.example`. Test keys are generated in memory.
- **Token lifetime:** 15 minutes by default. `ACCESS_TOKEN_LIFETIME_MINUTES` accepts 1–60 in every environment, so
  production cannot silently run with long-lived tokens.
- **Required claims:**
  - `sub`, `company_id`, `role`, `iat`, `exp` and `jti` are all required.
  - Their types are checked strictly: UUIDs, a known role, integer timestamps, a UUID `jti`.
  - `jti` is a random UUID4, never sequential.
- **Signature verification:** every token is verified with the configured public key before any claim is used.
  These are all rejected:
  - a tampered payload: role escalation, another company, another user, an extended expiry;
  - an altered, removed or borrowed signature;
  - a token signed with another key.
- **Expiration enforcement:** `exp` is enforced with no leeway, and an `iat` in the future is rejected.
- **No password or hash claims:** tokens hold no password, hash, email, name or status. Claims come only from the
  `AuthenticatedUser` that `authenticate()` returns, never from client input.
- **Generic failure:** every invalid token raises the same `AccessTokenError("Invalid or expired token")`, without
  the library's exception attached. Logs record the user id and `jti` on issue, and only the exception class name
  on rejection. They never contain the token, its claims or keys.

**Not implemented:**
- refresh tokens;
- logout or revocation: a token stays valid until it expires, and there is no blacklist or token store;
- key rotation;
- `iss`/`aud` claims;
- re-checking user status, company status or role on each request;
- the HTTP authentication boundary.

This is not full session security.

## 5. Role-based access control (Phase 1.4)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.4 and D-018 in [DECISIONS.md](DECISIONS.md)):

- **Role/permission mapping:**
  - `app/rbac.py` `ROLE_PERMISSIONS` is the only place a permission is granted. It is immutable at runtime.
  - `COMPANY_ADMIN` has `company:read`, `company:user:read`, `company:user:manage`, `policy:read`,
    `policy:manage`, `search:use` and `chat:use`.
  - `STAFF` has `policy:read`, `search:use` and `chat:use`.
  - Tests pin the full matrix, and also show that grants follow the mapping only: there is no hidden "admin can do
    anything" rule.
- **Default deny:** each of these is denied, and nothing falls back to `STAFF` or `COMPANY_ADMIN`:
  - an unknown, missing or non-member role (a plain string, another case, padding);
  - a role missing from the mapping or mapped to a malformed value;
  - an unknown, empty, wildcard or plain-string permission;
  - any object that is not a `TokenIdentity`.
- **401 vs 403:**
  - A missing, empty or non-Bearer `Authorization` header is **401** "Not authenticated".
  - An invalid, expired, forged or unsigned token is **401** "Invalid or expired token".
  - A valid token whose role lacks the permission is **403** "Permission denied".

  Bodies are `{"detail": ...}` only and name no permission, role or reason. A denial is logged with the user id and
  the permission name only.
- **No client-controlled role:**
  - The identity comes only from the verified token (`TokenIdentity`).
  - `require_permission(identity, permission)` takes no role or company argument.
  - A role, permission list or `company_id` in the body, query string or headers is never consulted.
- **Privilege-escalation protections:** tests show that each of these stays STAFF (403) or is not authenticated at
  all (401):
  - a STAFF token sent with `role=COMPANY_ADMIN` in the body, query or headers;
  - a STAFF token whose payload is changed to `COMPANY_ADMIN` (the signature fails);
  - an unsigned admin token;
  - an admin token signed with another key;
  - an attempt to change a `TokenIdentity` role in place (refused: the model is frozen).
- **RBAC is not tenant isolation:**
  - A `COMPANY_ADMIN` of company A holds `company:user:manage`.
  - Nothing yet checks that a requested user, policy or document belongs to company A. That is tenant
    authorization (Phase 1.5).
  - Threats T1 and T13 are therefore still open.

**Not implemented:**
- tenant resolution or authorization;
- re-checking the role against the database;
- per-company custom roles;
- protected business endpoints, since none exist yet.

## 6. Tenant resolution (Phase 1.5)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.5 and D-019 in [DECISIONS.md](DECISIONS.md)):

- **Authoritative tenant source:**
  - The request's tenant is `TokenIdentity.company_id` from the verified access token, and nothing else.
  - `resolve_tenant(identity)` (`app/tenancy.py`) and the dependency `current_tenant` (`app/api/tenancy.py`) build
    a `TenantIdentity(company_id)` from it. It holds no password, hash, token or personal data.
  - Resolution does not query the database, and `current_tenant` reuses `current_identity`. There is no second
    token validation, and it reads no headers.
- **Client tenant ids are ignored:**
  - A `company_id` in the path, query string, body, an `X-Company-Id` header or a cookie reaches the route but never
    changes the tenant.
  - Tests cover each transport and all at once. User A claiming B resolves A; user B claiming A resolves B.
  - Empty, malformed, nil or path-like values are ignored, not used as a fallback.
  - The resolver takes no company argument.
- **Forged-token protection:** these are all rejected with 401 before any tenant exists:
  - a token whose `company_id` was edited (signature fails);
  - a token signed with another key;
  - `alg=none`;
  - an expired token.
- **Fail closed:**
  - These all fail: an identity that is not a `TokenIdentity` (including an `AuthenticatedUser`, a dict or a bare
    UUID), a missing company id, a malformed one (string, bytes, int, None) and the nil UUID.
  - Over HTTP, a validly signed token with a malformed or nil company id is 401 with the generic invalid-token
    response.
  - A warning is logged with the user id only.
  - There is no default, first, empty, global or client-supplied tenant.
- **Tenant resolution is not tenant authorization.**
  - It establishes which company the request acts for. It does not compare a requested company id with the token's.
  - It does not decide whether a resource belongs to the tenant, and does not filter queries.
  - RBAC (§5) stays separate.
- **Logging:** tokens, `Authorization` headers and request bodies are never logged. The request log records the URL
  path by design, so a company id placed in a path appears there; query, body, header and cookie values do not.

**Remaining tenant-isolation work:**
- **Phase 1.6:** tenant-aware database access, so queries cannot run without the resolved tenant; possibly
  PostgreSQL row-level security.
- **Later:** resource-ownership checks on every tenant-owned record, and tenant-isolation tests with a zero-leakage
  target.

**Threat status:**
- T13 (frontend tenant manipulation) is mitigated for tenant *selection*.
- T1 (cross-tenant leakage) stays open until data access enforces the tenant (§7).

## 7. Tenant-aware database access (Phase 1.6)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.6 and D-020 in [DECISIONS.md](DECISIONS.md)):

- **Tenant-aware database access:**
  - Tenant-owned data is queried only through `TenantDatabase(conn, tenant)` (`app/tenant_db.py`).
  - It accepts only a valid `TenantIdentity`, and has no method that takes a company id and no raw `execute`.
  - It is immutable.
  - Today it covers `users`, the only tenant-owned table, through `list_users()` and `get_user(user_id)`.
- **Company id from the trusted `TenantIdentity`:** every query filters `WHERE company_id = %(company_id)s`, with
  `TenantIdentity.company_id` bound after all other parameters, so no parameter can replace it. SQL is literal, with
  bound parameters only, and tenant reads never select `password_hash`.
- **No client-controlled tenant selection:** the layer never sees request data. Over HTTP, a STAFF user of company A
  sending company B's id by path, query, body, header, cookie or all at once still gets only A's users. A token
  whose company id was changed to B is a 401 before any query.
- **Fail closed:**
  - These are all refused before any query: `None`, a bare UUID, a string, a `TokenIdentity`, an
    `AuthenticatedUser`, a request payload, and a `TenantIdentity` with a missing, NULL, string or nil company id.
  - A tenant with no users, or an unknown tenant, gets an empty result, never a fallback.
  - As defence in depth, a row outside the tenant raises instead of being returned.
- **Cross-tenant isolation tests** run against PostgreSQL with companies A (A1, A2) and B (B1, B2):
  - A sees exactly A1 and A2, and B sees exactly B1 and B2; the sets are disjoint.
  - A asking for B's users by id gets nothing, and the reverse.
  - The chain from credentials to `authenticate()`, token, tenant and query reaches only the user's own company.
  - Text containing quotes and SQL fragments is stored and returned as data.
- **COMPANY_ADMIN cannot bypass the tenant boundary:** the layer has no role input. Company A's admin holds
  `company:user:read`, but sees only A's users and cannot fetch B's by id. There is no super-admin or cross-company
  access.
- **Separation:** the database layer imports no web framework, JWT library, token or RBAC module (a test checks the
  imports), reads no headers or cookies, and never reads a role.
- **Deliberate-break check:** removing the filter (with or without the defensive row check), a caller-supplied
  company id, a default tenant, an unscoped missing tenant, a wrong tenant id and an admin bypass were each caught
  by the tests.

**Remaining work:**
- **API-level authorization:** routes combining `permission_required`, `current_tenant` and `TenantDatabase`, so
  that each route enforces all three (Phase 1.7).
- **Resource-ownership rules** beyond the tenant.
- **Broader cross-tenant tests** once policy and document tables exist (Phase 1.8 and later).
- **Production connection management.**
- **PostgreSQL row-level security,** as optional hardening.

**Threat status:** T1 (cross-tenant leakage) is now enforced at the data-access layer for existing tenant-owned data.
It stays open overall until every tenant-owned table and route uses the boundary.

## 8. API authorization (Phase 1.7)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.7 and D-021 in [DECISIONS.md](DECISIONS.md)):

- **Protected API authorization:** a protected route declares `Depends(permission_required(Permission.X))`.
  - `create_app()` refuses to start if any route declares no permission, except GET/HEAD on `PUBLIC_PATHS`
    (`/`, `/health`) and FastAPI's documentation pages. Authentication-only or tenant-only routes count as missing
    one. Since Phase 1.8 (§9), so do plain Starlette routes, websockets and mounted apps.
  - A test shows the real app has none, and that the check finds declarations on routes, parameters, routers and
    `include_router`.
- **Permission-based access:** the decision is `TokenIdentity.role` → `ROLE_PERMISSIONS` (unchanged).
  - Tests derive the 14-case matrix from the real mapping, over full protected routes (permission + tenant).
  - COMPANY_ADMIN gets all seven permissions; STAFF gets `policy:read`, `search:use` and `chat:use`.
- **401 vs 403:**
  - A missing, malformed, invalid or expired token is 401, and neither RBAC nor tenant resolution runs.
  - A validly signed token with a non-existent role is also 401, because it never becomes an identity.
  - A valid identity without the permission is 403. So is an authenticated role missing from the mapping, a
    permission granted to no role, and `require_permission()` raising inside a route: the central handler maps
    `AuthorizationError` to 403 with no traceback, where it used to be a 500.
  - A non-`Permission` value cannot be declared at all (`TypeError`).
- **Client role/company ids are untrusted:**
  - A role or permission list sent by query, header, cookie or body never elevates STAFF.
  - A `company_id` sent by any of those changes neither the decision nor the tenant.
  - The permission check's only input is `current_identity`; it has no query, path, header, cookie or body
    parameter.
- **COMPANY_ADMIN remains tenant-scoped:** on a route requiring `company:user:read`, with real data, company A's
  admin gets only A's users, even when sending company B's id everywhere. STAFF gets 403 and the database is never
  queried.
- **RBAC does not query the database:** the permission modules import no database code, and no connection is
  attempted during permission decisions.
- **No duplicate validation:** with `current_identity` replaced in a test, the role and the tenant follow it, and a
  protected request decodes the token exactly once.
- **Responses and logs:** 401/403 bodies are `{"detail": ...}` only and name no permission, role or module. Tokens
  are never logged.

**Remaining resource-authorization work:**
- resource-ownership checks per record (may this user use *this* policy or document);
- business routes and their permissions;
- the broader cross-tenant verification suite (Phase 1.8, §9).

## 9. Cross-tenant security verification (Phase 1.8)

**What it verifies:** that no attacker, client-controlled value or likely developer mistake lets a request belonging
to company A read company B's protected data. It tests the whole chain (see [ARCHITECTURE.md](ARCHITECTURE.md)
§2.8): JWT → `TokenIdentity` → RBAC → tenant resolution → `TenantDatabase` → rows.

**Threat model:**
- **Client-controlled tenant or role:** values in the path, query string, body, headers or cookies.
- **Forged or foreign credentials:** an edited company or role, another signing key, `alg=none`, HS256 keyed with
  the public key, a relabelled header, an expired token.
- **Missing or malformed authentication.**
- **Unknown roles and permissions, and invalid tenants:** a nil or malformed company in a validly signed token.
- **Privilege escalation:** STAFF using admin operations.
- **Cross-tenant id guessing:** an admin asking for another company's user by id.
- **Developer mistakes:** an unprotected route, a tenant built from client input, an unscoped query, a bypassed
  tenant layer.

**Database-backed adversarial tests:** `tests/database/test_cross_tenant.py`, 54 tests, all marked `security` and
`database`. They use companies A {admin, staff} and B {admin, staff}, test-only routes using the real dependencies,
and a recorder of the tenant of every query `TenantDatabase` executes.
- **Whole chain from a real login** (`authenticate()` → token → HTTP → RBAC → tenant → database):
  - each company's admin gets exactly its own users, and the only query runs for its own company;
  - the same admin with the other company claimed in every transport, plus `role=COMPANY_ADMIN`, still gets only
    its own users, though the claims reached the route;
  - STAFF reads its own company, and its admin attempt is 403 with no query.
- **Client tenant never overrides the token tenant** (A–G, T): 24 cases. Both directions × path / query / body /
  header / cookie / all × an admin route and a STAFF route. Each returns only the token company's rows, from one
  query bound to the token's company.
- **STAFF with the other company's id and admin claims** (H, I, V): 403, and no query ran.
- **Credential attacks** (J–P, Q, S): 11 credential attacks and 5 missing or malformed headers. Each is 401, with no
  query and no data in the response.
- **Unknown role or permission** (Q, R): an authenticated role missing from the mapping, or a permission granted to
  no role, is 403 with no query. An undeclared permission cannot even be used on a route.
- **Admin id-guessing across tenants** (U): both directions return nothing for the other company's users, and every
  lookup ran inside the token's company.

**Structural verification (D-022):**
- `tests/unit/test_security_structure.py` checks the parsed application source. Tokens are verified only by
  `current_identity`; each identity type has one construction site; no request data is read; every statement on a
  tenant-owned table is scoped; the tenant layer executes SQL only through `_fetch`; no code compares roles; boundary
  functions take no role or company argument.
- `tests/api/test_route_security.py` checks the routes: the guard regressions, and no real route taking a
  tenant/role/permission parameter in any transport.

**Security flaw found and fixed:** the Phase 1.7 startup guard let these start with no permission declared:
- an unprotected `POST /health` or `DELETE /` (the public paths were exempt for every method);
- a plain Starlette route, a websocket or a mounted app (non-API routes were skipped).

Now public paths are exempt for GET/HEAD only, and any other non-API route except FastAPI's documentation pages is
refused (D-022). There are regression tests for each case.

**Deliberate-break verification:** 11 breaks, each run against the full suite on a scratch copy. Every one was
caught:
- tenant from client company id;
- tenant filter removed;
- STAFF given admin permissions;
- client role trusted;
- client company id trusted at authentication;
- denial continuing to the database;
- unknown roles allowed;
- missing authentication allowed;
- a tenant built from a path parameter, caught by the structural test alone;
- the tenant layer bypassed;
- the old guard blind spots restored.

**Threat status:**
- **T13** (frontend tenant manipulation): mitigated for tenant selection, authorization and data access on the
  existing boundary.
- **T1** (cross-tenant leakage) and **T12** (privilege escalation): verified for the only tenant-owned data that
  exists (`users`). They remain open overall until policies, documents, chunks, vectors and their routes exist and
  are covered the same way.

**Remaining limitations:**
- no resource ownership;
- no PostgreSQL row-level security (the boundary is application-level);
- no production connection management;
- role and tenant can be up to one token lifetime stale;
- code inside the application can still build identities directly; the structural tests reduce, but cannot
  eliminate, this.

## 10. Final Phase 1 security verification (Phase 1.9)

**The question:** within the current architecture, is there any practical way for an unauthenticated client,
another tenant or an accidental developer implementation to get past the Phase 1 boundary? It covers the whole
chain (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.8): request → authentication → `TokenIdentity` → RBAC →
`TenantIdentity` → `TenantDatabase` → rows.

**The answer:**
- **Clients and other tenants:** no route found. Every attack class below is blocked and covered by a test.
- **Developer mistakes:** four were not yet caught by any check: a permission declared after other work, a route
  added after the guard, an identity copied with new fields, and environment-dependent behaviour. Each is now
  pinned by a test (D-023).
- **Production code:** no change was needed.

**Review by area** (every Phase 1 module re-read):
- **A. Authentication:**
  - The only authentication path is `current_identity` (FastAPI `HTTPBearer` → `decode_access_token`).
  - RS256 is a code constant, and `exp` and every claim are required.
  - Missing, malformed, expired, wrong-key, `alg=none` and HS256-with-public-key tokens all fail closed with 401.
  - 30 fuzzed malformed tokens all raised the generic `AccessTokenError`: bad base64 or JSON, non-object headers,
    odd `alg` values, `crit`, `jku` and `jwk` headers, a 200 KB token.
  - A valid token sent anywhere but `Authorization: Bearer` authenticates nothing.
- **B. Identity integrity:**
  - `TokenIdentity` is built only from verified claims, and `TenantIdentity` only from a `TokenIdentity`.
  - Both are frozen, and neither is copied with new fields or built unvalidated.
  - No client company, role or permission value is read.
- **C. RBAC:**
  - The only grant source is `ROLE_PERMISSIONS`, an immutable mapping.
  - Default deny applies to unknown roles, unknown permissions, strings and non-identities, with no fallback role
    and no role comparisons.
- **D. Tenant:**
  - The tenant is the verified company id only; the nil UUID and malformed ids fail closed.
  - There is no client, default or widened tenant, and no second construction path.
- **E. Database:**
  - SQL on `users` is scoped in `TenantDatabase` and executed only through `_fetch`; a stray row raises.
  - Login (by company slug) is the only other reader.
  - A denied request runs no query, and no other dependency runs before the permission check (D-023).
- **F. Routes:**
  - The startup guard reports every route without a permission, including non-API routes, websockets and mounts.
  - The public exemption is GET/HEAD on `/` and `/health`, served only by the two dependency-free endpoints.
  - The served app is checked too.
  - Authorization is attached to the route object, not to URL matching, so path tricks cannot skip it.
- **G. Leakage:**
  - 401 and 403 bodies are fixed and generic; unexpected errors give a generic 500 with no traceback, including
    when the keys are missing.
  - No password hash leaves login.
  - Tokens, claims and keys are never logged, the private key is redacted, and request logs hold metadata only.

**Configuration and secrets:**
- **Keys come from configuration.** They are validated at startup (unencrypted PEM RSA, at least 2048 bits, a
  matching pair) and required in production.
- **The algorithm is fixed** in code.
- **Token lifetime** is configured, from 1 to 60 minutes (15 by default).
- **Nothing is hardcoded:** no key or production secret is in the code or the `.env.example` files, and `.env` is
  git-ignored.
- **No silent production bypass:**
  - Without keys, protected requests fail with a 500 rather than letting anyone in.
  - No authentication or authorization behaviour depends on `ENVIRONMENT` or `DEBUG`, and `DEBUG` never reaches
    FastAPI.

**Dependencies and infrastructure:**
- **Packages:**
  - No dependency was added in Phases 1.4–1.9; PyJWT[crypto] and cryptography were added in 1.3.
  - `pip check` is clean.
  - All 38 installed packages had no known advisories in the OSV database (2026-11-12).
- **Docker:**
  - ports are published on 127.0.0.1 only;
  - the backend runs as a non-root user;
  - `.env` is excluded from the image build;
  - credentials come from `.env`, and no keys are passed to the container.

**Final attack coverage** (✓ = blocked and covered by tests; "1.x" is the phase whose tests cover it):

| # | Attack | Result | Covered by |
|---|---|---|---|
| 1 | Missing authentication | 401, nothing runs | 1.4, 1.7, 1.8 (O) |
| 2 | Malformed authentication | 401 | 1.4, 1.7, 1.8 (P); 1.9 fuzzing |
| 3 | Expired token | 401 | 1.3, 1.7, 1.8 (N) |
| 4 | Wrong signing key | 401 | 1.3, 1.4, 1.8 (L) |
| 5 | Algorithm confusion (`alg=none`, HS256 with the public key, relabelled header) | 401 | 1.3, 1.8 (L, M) |
| 6 | Forged `company_id` | 401 | 1.3, 1.5, 1.8 (J) |
| 7 | Forged role | 401 | 1.3, 1.4, 1.8 (K) |
| 8 | Client role injection | ignored; STAFF stays STAFF | 1.4, 1.7, 1.8 (C–G, H, I) |
| 9 | Client `company_id` injection | ignored; token tenant only | 1.5, 1.7, 1.8 (A–G, T) |
| 10 | Client permission injection | ignored; 403 | 1.4 (query, body); **1.9** (query, header, cookie, body) |
| 11 | STAFF → admin escalation | 403, no query | 1.4, 1.7, 1.8 (H, I, V) |
| 12 | Unknown role | 401 (signed) / 403 (unmapped) | 1.3, 1.4, 1.7, 1.8 (Q) |
| 13 | Unknown permission | 403; undeclarable | 1.4, 1.7, 1.8 (R) |
| 14 | Invalid tenant (nil, malformed) | 401 | 1.5, 1.6, 1.8 (S) |
| 15 | Tenant mismatch (token A, claims B) | token tenant wins | 1.5, 1.7, 1.8 (A–G) |
| 16 | Cross-tenant user lookup | nothing returned | 1.6, 1.8 (U) |
| 17 | Cross-tenant list | own rows only | 1.6, 1.7, 1.8 (whole chain) |
| 18 | Database bypass | structural + behavioural | 1.6, 1.8 structural; breaks |
| 19 | Missing tenant SQL scope | structural + behavioural | 1.6, 1.8 structural; breaks |
| 20 | Authorization bypass (work before the check) | nothing runs before the check | 1.8 (no query on denial); **1.9** (permission-first) |
| 21 | Missing route permission | app refuses to start | 1.7, 1.8; **1.9** (served app) |
| 22 | Non-API route bypass | refused | 1.8 |
| 23 | Mounted-route bypass | reported | 1.8 |
| 24 | Websocket bypass | refused | 1.8 |
| 25 | Sensitive error leakage | generic 401/403/500 | 0.6, 1.3, 1.4, 1.7, 1.8 |
| 26 | Credential/hash leakage | never returned | 1.1, 1.2, 1.6; **1.9** (only login reads hashes) |
| 27 | Token logging | never logged | 1.3, 1.4, 1.5, 1.7 |

**Added in Phase 1.9** (19 tests, all `security`):
- **API:** a valid token anywhere but a bearer `Authorization` header (8), and client permission injection in each
  transport (4).
- **Routes:**
  - the permission-first behaviour, with a positive control proving the order check matches FastAPI;
  - the order rule on the real app;
  - the served app checked like `create_app()`;
  - the public endpoints pinned.
- **Structure:**
  - no identity copy with changed fields, or unvalidated construction;
  - no `ENVIRONMENT`/`DEBUG` branches;
  - only login reads `password_hash`.

**Deliberate-break verification:** 17 breaks, each run against the full suite (644 tests, database included) on a
scratch copy, then restored. Every one was caught. The unmodified copy passed 644/644, before and after.
- **The 10 required breaks:** permission check bypassed; client role trusted; client `company_id` trusted; tenant
  built from client input; tenant SQL scope removed; `TenantDatabase` bypassed; missing authentication allowed;
  unknown role allowed; cross-tenant user lookup allowed; route protection disabled.
- **7 extras aimed at the Phase 1.9 tests:**
  - a data dependency declared before the permission;
  - a route added to the served app after `create_app()`;
  - an identity copied with a client company;
  - RBAC skipped when `DEBUG` is on;
  - token expiry not checked in development;
  - a token accepted from a cookie;
  - a second module reading password hashes.

  Four of them (the served-app route, the identity copy, the development-only expiry and the hash reader) were
  caught by the new tests alone. The development-only expiry break shows why: behavioural tests run with
  `ENVIRONMENT=test` and never see it.

Counts and the catching tests are in [IMPLEMENTATION_LOG.md](IMPLEMENTATION_LOG.md) (Task 1.9).

**Phase 1 exit criteria:** all met.
- **Verified:** authentication, JWT handling, RBAC, tenant resolution and tenant database isolation.
- **Attacks:** cross-tenant attacks are blocked, and client-supplied identity or tenant values are ignored.
- **Routes:** protected routes require permissions, and authorization precedes protected database access.
- **Tests:** structural checks exist, deliberate breaks are detected, and the full suite passes.
- **Open issues:** no critical Phase 1 flaw remains.

**Threat status at the end of Phase 1:**
- **T12** (privilege escalation) and **T13** (frontend tenant manipulation): mitigated for the Phase 1 boundary.
- **T1** (cross-tenant leakage): verified for `users`. Policies, documents, chunks, vectors and their routes must
  get the same coverage, and must be added to `TENANT_OWNED_TABLES`.
- **T2** (unauthorized document access): open until Phase 2.

**Remaining limitations:**
- **Coverage:** only `users` is tenant-owned today.
- **Enforcement:** application-level only (no PostgreSQL RLS), and no resource ownership beyond the tenant.
- **Not built yet:** production connection management, the login endpoint, refresh tokens and revocation.
- **Staleness:** role, status and tenant can be up to one token lifetime (15 minutes by default) out of date. A
  deactivated user keeps access until the token expires.
- **In-process trust:** the structural checks target accidental mistakes, not deliberately hostile code. They scan
  `app/` only, and the permission-first rule is a test, not a startup check.
- **Phase 2 cautions:**
  - FastAPI's 422 responses echo the client's own input back to that client. A login or user-creation route
    should not echo passwords.
  - PostgreSQL error details on writes to `users` include the failing row, so they must not be logged (CLAUDE.md
    database rules).
- **Phase 14 hardening:** the API documentation pages are public in every environment, and there is no rate
  limiting, lockout or MFA.

## 11. Tenant ownership of policies and documents (Phase 2.1)

What is implemented (see [ARCHITECTURE.md](ARCHITECTURE.md) §2.9 and D-024 in [DECISIONS.md](DECISIONS.md)):

- **Two new tenant-owned tables:** `policies` and `policy_documents`. Both carry `company_id`, and both are in
  `TENANT_OWNED_TABLES`, so the structural test checks that all their SQL is tenant-scoped.
- **Same-company rule in the database:**
  - The composite foreign key `(company_id, policy_id) → policies (company_id, id)` makes PostgreSQL refuse a
    document whose policy belongs to another company. That holds on insert, when a document's `company_id` or
    `policy_id` is changed, and when a policy with documents is moved to another company.
  - It does not depend on application code.
- **No shared files:** `storage_reference` is unique across all companies, so two tenants' records can never point
  at one stored file. File content is never stored in PostgreSQL.
- **Untrusted metadata is constrained:** `original_filename` cannot be a path (no `/`, `\` or control characters);
  `content_type` is `application/pdf` only; size and checksum formats are checked.
- **Tenant-scoped reads only:** `TenantDatabase.list_policies()`, `get_policy(id)`, `list_documents(policy_id)` and
  `get_document(id)`.
  - Each statement filters `company_id = %(company_id)s`, bound from `TenantIdentity`, and a stray row raises.
  - Ids must be UUIDs, and no method takes a company, tenant or role argument; a unit test covers every public
    method.
  - Another company's policy or document id returns exactly what an unknown id returns: nothing.
- **No deletes in cascade:** `ON DELETE RESTRICT` on every foreign key, so documents and versions cannot vanish with
  their policy or company.
- **Unchanged:** the tenant still comes only from the verified token (D-019) through `TenantIdentity`, with no RLS
  (D-020). No route reads these tables yet.

**Verification (PostgreSQL):**
- **Isolation:** companies A and B, where B reuses A's policy number and document key. Each lists only its own
  policies and documents. Neither can get the other's policy or document by id, or list the documents of the other's
  policy, in either direction. The tenant comes from a `TokenIdentity` through `resolve_tenant`.
- **Database rule:** inserting a document of company A against B's policy fails with `policy_documents_policy_fkey`.

**Deliberate breaks:** 11, each run against the full suite (773 tests) on a scratch copy. All were caught; the
unmodified copy passed before and after.
- **Unscoped reads:** `get_document` without its company predicate or row guard; `list_policies` without its scope;
  `list_documents` filtered by policy only.
- **Weakened schema:**
  - the composite foreign key replaced by `policy_id` alone;
  - policy numbers made globally unique;
  - storage references made unique per company only;
  - checksums made unique;
  - versions made unique per policy only;
  - `ON DELETE CASCADE`.
- **Drift:** a document-status value outside the enum.
- **Caller-chosen tenant:** a `company_id` parameter on `list_documents`.

**Threat status:** T1 (cross-tenant leakage) is verified at the data layer for `users`, `policies` and
`policy_documents`. It stays open overall until routes, storage, chunks and vectors exist and are covered the same
way. T2 (unauthorized document access) is open: there is no document endpoint yet.

**Remaining limitations:**
- **No RLS:** enforcement is still application-level.
- **Company changes:** a policy without documents could still be moved to another company by a direct UPDATE. No
  code does this, and Phase 2.2 must not offer it.
- **Storage layout:** storage references have no tenant prefix rule yet. The layout is decided with the storage
  backend.
- **Reads only:** writes (creating policies, recording uploads) arrive in later Phase 2 tasks and must go through
  `TenantDatabase`. Policy writes arrived in Phase 2.2 (§12).

## 12. Tenant-scoped policy writes (Phase 2.2)

What is implemented: `TenantDatabase.create_policy`, `update_policy` (number, name, insurance type),
`set_policy_status` and `delete_policy`. There is no API yet, so no route reaches them.

**Rules, and what tested each** (`tests/database/test_tenant_policy_writes.py` against PostgreSQL, unit tests with a
recording connection, structural tests):
- **The company comes only from `TenantIdentity`.**
  - No write method has a `company_id` parameter; passing one raises `TypeError` before any SQL (unit and
    database tests).
  - The INSERT stores the bound tenant. A created policy has the tenant's `company_id`, checked in the stored row,
    including when the tenant comes from a `TokenIdentity` via `resolve_tenant`.
- **UPDATE and DELETE are scoped in the SQL itself:** `WHERE company_id = %(company_id)s AND id = %(policy_id)s`.
  - A structural rule requires this for every UPDATE, DELETE and SELECT on a tenant-owned table, and requires that
    an INSERT's company is the bound tenant.
  - It forbids assigning `company_id` in a SET clause.
  - A positive control shows the rule rejects eight unsafe statement shapes.
  - A deliberate break that checked ownership in Python and then ran an unscoped UPDATE was caught by this rule.
- **Cross-tenant writes change nothing.**
  - Company A updating, re-statusing or deleting each of B's policies (and B doing the same to A's) gets `None` /
    `False`, and the victim row is unchanged in every column, `updated_at` included.
  - A guessed foreign id gives exactly the same results as a random unknown id.
- **No tenant move and no new primary key:** `update_policy` has no `company_id`, `id` or `status` parameter, and the
  updated row keeps its id and company.
- **Invalid tenants are refused at construction,** before any write: a duck-typed object, a dict, a
  `model_construct`-ed `TenantIdentity` with a text or nil company, and `None`. A `TenantIdentity` cannot be changed
  (frozen), and neither can the database object's tenant (immutable). After these attempts, B's policy count is
  unchanged.
- **Constraints still apply through the methods:**
  - an invalid number or name raises the database's `CheckViolation`;
  - a duplicate number in the tenant raises `UniqueViolation`, while the same number in another tenant succeeds;
  - text instead of an `InsuranceType` or `PolicyStatus` raises `TypeError` before the database.
- **Deletes respect document ownership.** Deleting a policy that has documents raises `RestrictViolation`, and the
  policy and both document versions remain. It is never reported as "not found", and nothing cascades.
- **Errors:** another tenant's policy is never revealed, because only the caller's own rows can fail a constraint.
  Database errors propagate unchanged for the future API layer to map. The global handler already returns a generic
  500 for anything unmapped.

**Deliberate breaks:** 10, each run against the full suite (838 tests). All were caught:
- unscoped UPDATE, DELETE or status update;
- a create that takes the company from the caller;
- a latent `SET company_id`;
- check in Python, then an unscoped UPDATE;
- an application-level cascade;
- "has documents" reported as "not found";
- the vocabulary type checks removed;
- the row guard skipped on writes.

**Remaining limitations:**
- **No RBAC at this layer:** a future route must declare `policy:manage`.
- **Direct SQL can still move a policy:** the database would accept moving a policy without documents to another
  company by a direct UPDATE. No code path can do it, and the structural rule forbids writing one.
- **Weak scope check:** the rule is textual, so an `OR` in a WHERE clause would pass it. *Closed in Phase 2.3
  (§13): `OR` is now refused, and an UPDATE or DELETE must filter on exactly the tenant and one id.*
- **No audit:** there is no audit trail of writes yet.

## 13. Tenant-scoped policy-document writes (Phase 2.3)

What is implemented: `TenantDatabase.create_document`, `update_document` (display file name only),
`set_document_status` and `delete_document`. They write metadata only, and no route reaches them yet.

**Rules, and the tests that verify each.** The database tests are in
`tests/database/test_tenant_document_writes.py` (PostgreSQL), the unit tests in `tests/unit/test_tenant_db.py`
(recording connection), and the structural tests in `tests/unit/test_security_structure.py`.

- **A document is created only under the tenant's own policy.**
  - The INSERT stores the bound tenant's `company_id` with the caller's `policy_id` beside it. The composite
    foreign key `policy_documents_policy_fkey` then refuses another tenant's policy.
  - **Tested:**
    - A and B each create under their own policy and fail under the other's (`ForeignKeyViolation` on that
      constraint), with neither company's document count changing
      (`test_a_tenant_creates_documents_only_under_its_own_policies`).
    - The exact SQL shape is pinned (`test_document_writes_bind_the_tenant_and_never_format_values_into_sql`).
    - A structural rule requires `company_id` and `policy_id` to be the first two bound columns of a
      `policy_documents` INSERT.
    - A positive control rejects an INSERT ... SELECT that copies a looked-up policy's company.
- **A foreign policy is indistinguishable from a missing one.**
  - **Tested:** creating under B's policy and under a random id give the same SQLSTATE, constraint, table, message
    and detail once the id is masked. The detail names only A's own company and the id A sent, never B's company
    (`test_a_foreign_policy_id_fails_exactly_like_a_missing_one`).
- **Cross-tenant update, status change and delete change nothing.**
  - **Tested:** in both directions they return `None` / `None` / `False`, and the victim row is unchanged in every
    column (`test_another_tenants_document_cannot_be_updated_re_statused_or_deleted`).
  - **Tested:** a guessed foreign id gives exactly what a random id gives
    (`test_a_guessed_foreign_document_id_is_indistinguishable_from_a_missing_one`).
- **No company from the caller, and no move.**
  - Nothing can be passed for `company_id` (any method), or for `id` and `policy_id` (`update_document`). Each
    raises `TypeError` before any SQL, even when moving between two of the tenant's own policies.
  - **Tested:** `test_a_caller_cannot_supply_a_company_id`,
    `test_a_document_cannot_be_moved_to_another_policy_or_tenant_or_get_a_new_id`, and the unit test
    `test_document_writes_take_no_company_and_never_change_a_versions_identity_or_file`, which covers all ten
    fixed fields.
- **Invalid or altered tenants cannot write.**
  - A nil, text or `None` company fails `TenantIdentity` validation.
  - A duck-typed object, a dict, a `model_construct`-ed text or nil company, and `None` are refused by
    `TenantDatabase`.
  - Changing a frozen identity or the database object's tenant fails. Afterwards, writes still land in A, and B's
    policy stays out of reach.
  - **Tested:** `test_an_altered_or_invalid_tenant_identity_cannot_write_documents`.
  - **Tested:** writes follow `TokenIdentity → resolve_tenant` in both directions
    (`test_document_writes_follow_the_verified_identitys_tenant`).
- **A version is fixed once recorded (D-025).**
  - Only the file name and the status change in place. A new file, key or version is a new row.
  - A delete removes one version, never its policy or its siblings, and nothing cascades.
  - **Tested:**
    - the update, status and delete tests compare the stored rows column by column;
    - `test_a_policy_can_be_deleted_only_after_its_documents_are`;
    - `test_versions_are_stored_as_given_and_earlier_versions_are_untouched`, which shows there is no superseding.
- **The status vocabulary stays `UPLOADED` / `INACTIVE`.**
  - **Tested:** text, `ACTIVE`, `SUPERSEDED` and a `PolicyStatus` raise `TypeError` before the database
    (`test_only_document_statuses_are_accepted`).
- **The metadata constraints stay authoritative.**
  - **Tested:** 18 invalid values raise the database's own `CheckViolation`
    (`test_invalid_metadata_is_rejected_by_the_database`). Duplicate versions raise `UniqueViolation`. A storage
    reference cannot be shared with another tenant (`policy_documents_storage_reference_key`).
- **The row guard holds on writes.**
  - **Tested:** a create, update, status change or delete answered with another tenant's row raises
    `TenantScopeError` (`test_a_document_write_answered_with_another_tenants_row_is_refused`).
- **The structural SQL rule is stronger.**
  - An UPDATE or DELETE's whole WHERE clause must be `company_id = %(company_id)s AND id = %(<x>_id)s`.
  - No WHERE clause may contain `OR`.
  - No statement may start with anything but SELECT/INSERT/UPDATE/DELETE, which refuses a CTE.
  - No SET may assign `company_id`, `id` or `policy_id`.
  - **Tested:** a new positive control rejects nine unsafe shapes, and a test confirms the scan finds the document
    writes. Every existing statement passes the stricter rule.

**Deliberate breaks:** 12, each run against the full suite (920 tests) on a scratch copy. All were caught; the
unmodified copy passed before and after.
- **Unscoped writes:** an unscoped update, status change or delete.
- **Company taken from outside the tenant:** a create that takes the company from a policy lookup, or from a caller
  parameter.
- **Check-then-write:** a check in Python, then an unscoped UPDATE.
- **A version changed in place:** a document moved to another policy, or its file overwritten.
- **Version history changed:** a delete that cascades to sibling versions, or a create that supersedes earlier
  versions.
- **Weakened checks:** the status type check removed, or the row guard skipped.

**Remaining limitations:**
- **No RBAC or HTTP mapping at this layer.** A future route must declare `policy:manage`. It must map
  `ForeignKeyViolation` on create to the same "not found" as a missing policy, and constraint errors to generic
  responses.
- **Storage references must be generated by the server.** They are unique across all companies, so a
  client-chosen value would let one tenant learn that another's reference exists. The storage phase must not accept
  them from clients.
- **Immutability is application-level.** A direct SQL UPDATE could still change a version's key, number or file
  metadata.
  - No code path can.
  - The structural rule forbids SET on `company_id`, `id` and `policy_id`.
  - The other fixed columns are pinned by unit tests on `update_document`'s parameters and its exact SQL: break 8
    was caught there, not by the structural rule.
  - A database trigger would be a schema change, which this phase did not need.
- **The scope check is still textual:** it pins shapes, not semantics.
- **No audit trail** of document writes yet.

## 14. Policy and document API (Phase 2.4)

What is implemented: 11 routes in `app/api/policies.py` (ARCHITECTURE.md §2.10, D-026), metadata only.

**Rules, and the tests that verify each.**
- **API (database-free):** `tests/api/test_policy_api.py`.
- **API against PostgreSQL:** `tests/database/test_policy_api.py`, through the real chain with real tokens, and a
  real `authenticate()` login in one test.
- **Structural:** `tests/api/test_route_security.py`.

- **Authentication on every route.**
  - **Tested:** all 11 routes × 4 missing or malformed `Authorization` headers give 401 and open no database
    connection (`test_every_route_requires_authentication_before_the_database`).
  - **Tested:** the Phase 1.8 attack table gives 401 on a read and a write, with no connection, no query and no data
    in the response (`test_forged_expired_foreign_or_malformed_credentials_are_401_before_the_database`). It covers
    a forged company or role, the wrong signing key, `alg: none`, HS256 signed with the public key, a relabelled
    header, an expired token, an unknown role, a nil or malformed tenant, and a missing, scheme-only, other-scheme,
    garbage or padded header.
- **RBAC before any database work.**
  - STAFF gets 403 on all 7 management routes, even with `X-Role`, `X-Permission`, `role` or `permission` values in
    the headers, query or body. No connection is opened, no query runs and nothing changes.
  - **Tested:** `test_staff_cannot_manage_and_never_reaches_the_database` (API) and
    `test_staff_cannot_manage_and_the_database_is_never_reached` (PostgreSQL). A STAFF body carrying an admin role
    is a 403, not a 422 (`test_a_staff_body_carrying_an_admin_role_is_still_denied_first`).
  - STAFF reads all four read routes (`test_staff_reads_policies_and_documents`).
  - **Structural:** each route has exactly its expected permission, `policy:read` for GET and `policy:manage`
    otherwise, resolved before anything else, in both `create_app()` and the served app
    (`test_every_policy_and_document_route_is_served_with_its_permission_declared_first`).
  - **Startup guard:** a tenant-only route without a permission still stops `create_app()`
    (`test_create_app_refuses_a_tenant_route_that_forgot_its_permission`).
- **The tenant comes only from the token.**
  - Client company, tenant, role and permission values in the query, headers and cookies change nothing, and in a
    body they are a 422 with nothing created (`test_client_company_role_and_permission_values_never_change_the_tenant`,
    both companies).
  - **Structural:** every route reaches data through `current_identity → current_tenant → tenant_database →
    database_connection`
    (`test_every_policy_and_document_route_reaches_data_only_through_the_verified_tenant`). No route parameter is
    named like a tenant or role. Every body model forbids unknown fields and has no tenant, role, id, `policy_id` or
    `storage_reference` field (`test_request_bodies_reject_unknown_fields_and_carry_no_tenant_role_or_storage_field`).
- **Foreign resources are indistinguishable from missing ones.**
  - For A → B and B → A, GET, PATCH and DELETE of a policy (with and without documents), listing its documents and
    creating a document under it give exactly the response of a random id (404, same body). The victim rows are
    unchanged (`test_another_tenants_policy_is_indistinguishable_from_a_missing_one`).
  - The same holds for GET, PATCH, PUT status and DELETE of a document
    (`test_another_tenants_document_is_indistinguishable_from_a_missing_one`).
  - Creating a document under a foreign policy: same 404 as a missing policy, and no row anywhere
    (`test_a_document_cannot_be_created_under_another_tenants_policy`).
- **Storage references are server-only.**
  - A client `storage_reference`, even another tenant's real one, is a 422 and creates nothing.
  - Generated references are distinct, 43 URL-safe characters, and absent from every response
    (`test_the_storage_reference_is_generated_by_the_server_and_never_exposed`).
- **A recorded version stays fixed (D-025).** The document PATCH rejects key, version, checksum, size, content type,
  status, storage reference and id, and the row stays unchanged (`test_document_patch_changes_only_the_file_name`).
  A move to another tenant's policy is a 422 (`test_a_document_cannot_be_moved_to_another_tenants_policy`).
- **Responses carry no internals.**
  - Response models have no `company_id` or `storage_reference`
    (`test_responses_never_carry_the_company_or_storage_internals`). Listing returns no company id
    (`test_a_tenant_lists_exactly_its_own_policies_without_company_ids`).
  - Database refusals give fixed messages: 409 for duplicates (per tenant only) and for deleting a policy with
    documents; 422 naming only the field for CHECK and NUL errors. They contain no table, constraint, SQLSTATE or
    message text (`test_values_the_database_refuses_are_a_safe_422`, `test_invalid_document_metadata_is_a_safe_422`,
    `test_a_duplicate_*`, `test_a_policy_with_documents_is_a_conflict_to_delete`).
  - An unmapped database error is the generic 500 (`test_an_unmapped_database_error_is_a_generic_500`).
  - An unreachable database is the generic 500, with no database name, host, port or password
    (`test_an_unavailable_database_is_a_generic_500_without_connection_details`).
- **Transactions.**
  - Against a throwaway database with the real connection dependency, successful writes are visible to another
    connection, and refused ones leave nothing
    (`test_successful_writes_are_committed_before_the_response_and_refused_ones_leave_nothing`).
  - A route that writes and then raises (`HTTPException` or a crash) commits nothing
    (`test_a_request_that_fails_after_writing_commits_nothing`).
  - The commit or rollback happens before the response starts (`test_the_transaction_ends_before_the_response_is_sent`).
    Both dependencies are declared with `scope="function"` on every route (structural).

**Deliberate breaks:** 14, each run against the full suite on a scratch copy. All were caught; the unmodified copy
passed before and after.
- **Permissions:** a write declaring `policy:read`; a route with no permission (the app refuses to start); a
  permission checked after the database dependency.
- **Disclosure:** a foreign policy reported differently from a missing one; database messages in a 409 or 422;
  `company_id` or `storage_reference` in responses.
- **Untrusted input:** a client-chosen storage reference; bodies ignoring unknown fields; a client `company_id`
  honoured on list.
- **Transactions:** no transaction; the transaction ending after the response.
- **Not found:** an empty list instead of 404 for a foreign policy's documents.

The "after the response" break was first missed: the test client returns only once the whole request has finished.
The ordering test and the scope check were added for it.

**Threat status:** T2 (unauthorized document access) is mitigated for document metadata. Every document route is
authenticated, authorized and tenant-scoped. It stays open for file content, which does not exist yet.

**Remaining limitations:**
- **No connection pool:** each request opens a connection. A slow or unavailable database is a 500.
- **No rate limiting, request-size limit or audit records** (Phase 2.5 and Phase 14).
- **Timing:** a foreign id and a missing id give identical responses, but their timing was not measured.
- **Policy status after creation** has no route (`set_policy_status` is unused).
- **Generated storage references** point at nothing until storage exists.
- **The documentation pages are public** (Phase 1.9). They now list these routes, but not their data.

**Phase 2.5 review:** the boundary above was re-verified. No security behaviour needed changing. Gaps were closed
with focused tests only:
- **Client tenant values on document routes:** client company, tenant, role and permission values in the query,
  headers and cookies leave every response on another tenant's document or policy identical to the plain request
  (404), in both directions (`test_client_tenant_values_never_unlock_another_tenants_documents`).
- **Provenance through the status route:** `PUT /documents/{id}/status` rejects each of the 9 provenance and
  identity fields (`test_the_status_route_changes_only_the_status`).
- **Authentication before validation:** an anonymous request with a bad id or body is 401 before any validation
  (`test_anonymous_requests_are_401_before_any_validation`).
- **The single transaction owner:** only `app/api/database.py` opens or ends a transaction; no `commit` or
  `rollback` exists in `app/` (`test_only_the_api_database_dependency_owns_transactions`).
- **Known exception, accepted:** a body that is not valid JSON is rejected (422 `json_invalid`) before
  authentication, because FastAPI parses JSON before any dependency. The response carries only the parser's error
  about the client's own bytes and reaches no identity, tenant or database
  (`test_a_body_that_is_not_json_is_rejected_before_authentication_without_reaching_anything`). Changing this would
  mean replacing FastAPI's body handling in every route.

## 15. Cited answers: sufficiency, abstention and citations (Phase 9)

What `POST /chat` does about T3, T7, T8 and T10 (see [ARCHITECTURE.md](ARCHITECTURE.md) §6 and D-031):
- **The model points; the server cites (T7).** The model returns only a status, an answer and the labels (`E1`,
  `E2`, …) of the passages it relies on. Each citation is built by the server from the stored provenance of the
  retrieved chunk behind a label it issued for this prompt: chunk, document, policy, version, pages and section.
  - A label must be one issued for this prompt, appear once, and carry this prompt's tag if it carries one.
  - An unknown, out-of-range, repeated or foreign-tagged label, a page number, a chunk id or a UUID is refused, and
    the whole answer is withheld.
  - Page numbers, numbered references and "source:" lines in the answer text are refused too, so a planted
    "cite page 999" can never become a citation.
- **Abstention (T8, T10):** the fixed insufficiency sentence and no citations, when:
  - retrieval found nothing (the model is not called);
  - the model says no passage answers;
  - the verdict is malformed, cites nothing, or fails any check;
  - the answer states a number (amount, period, limit) that is in neither the cited passages nor the question.
  The internal reason is logged, never returned.
- **Conflicts** are reported with at least two cited passages, never resolved.
- **Client input:** the body is still `{question, top_k, filters}`. Any citation, evidence, page, chunk, document,
  company, status, answer, model, provider, prompt, grounding or schema field is a 422 that never reaches the model.
- **Tenant isolation is unchanged:** citations can only name passages of the tenant's own retrieval, and foreign
  filters abstain with no evidence and no model call.
- **Logs:** status, abstention reason, counts and timings only. Never the question, evidence or answer.
- **Limits:** verification is deterministic but not semantic. It proves that every citation is a real passage the
  model was shown and that every number is grounded, not that the cited passage entails the answer. A planted
  passage can still be cited as support for an answer it contains, if the model adopts it. Measured with qwen2.5:3b,
  planted instructions were never adopted (IMPLEMENTATION_LOG, Phase 9).

## 16. Sign-in, demo access and the frontend (Frontend & Final Integration, D-036)

See D-032, D-033, D-036 and [ARCHITECTURE.md](ARCHITECTURE.md) §10.
- **`POST /auth/login`, `POST /auth/register-company` and `POST /auth/demo-session` are the only public writes
  (T12, T13; D-035, D-036).**
  - The startup guard exempts exactly those paths and methods (`PUBLIC_WRITES`).
  - Structural tests pin their shape: one endpoint each, a database connection (registration's in a transaction)
    as the only dependency, and a body of exactly their own fields that forbids anything else (the demo session has
    no body, and reads no path, query, header or cookie).
  - Registration's single SQL statement is pinned too: the new admin's company is the id generated in that
    statement, and the role is the literal `COMPANY_ADMIN`.
  - A client `role`, `company_id`, `tenant_id` or `permissions` field is a 422. The same values in headers or the
    query are ignored: the token's company and role always come from the stored user.
- **Demo access (D-036) is a development convenience, never a production mode.**
  - It is off unless `DEMO_COMPANY_SLUG` and `DEMO_USER_EMAIL` are both set, and `Settings` refuses them when
    `ENVIRONMENT=production`. Off, the route is a 404 identical to a missing route.
  - On, anyone who can reach the API acts as that one configured user, with that user's stored role. The Compose
    ports and the dev server bind to `127.0.0.1` only.
  - Nothing the client sends can change the company, user or role: the route has no inputs, and the database tests
    send a company code, company id, role and tenant in the body, query, headers and a cookie and get the configured
    user's token anyway.
  - The token is an ordinary access token, so tenant isolation is unchanged: it reaches only the configured
    company's data (another company's policy is the same 404 as a missing one), and every other company remains
    reachable only with its own token.
  - The lookup does not read the password hash (a structural test checks that only the sign-in lookup does). A
    missing, inactive or other-company user is one generic 503; the reason is logged by user id only, never the email
    or token.
- **Credential failures are indistinguishable.**
  - An unknown company or user, a wrong password, or an inactive user or company: all the same 401 "Invalid
    credentials", with timing equalised by `authenticate()`.
  - A missing field fails the same way, never as a 422 that would echo the password.
  - Nothing logs the password, the email or the token.
- **The frontend holds no authority.**
  - Requests carry only the bearer token, never a tenant, role or permission. The tests and the end-to-end run check
    the request bodies.
  - The role only hides controls. The API tests check the backend's 403 for a STAFF write; the end-to-end run checks
    its 401 without a token and its 422 for a client `company_id`.
  - Server error text is never displayed: errors map to fixed messages.
- **Token handling (D-036).** The token is kept in memory only (never in `sessionStorage` or `localStorage`), sent
  only as a bearer header (`credentials: "omit"`), and never rendered; the tests check storage and the page for it.
  It is renewed before expiry, and once after a 401. Script on the page could still read it, so XSS remains the main
  risk to it; React escapes all text and the page injects no HTML.
- **Operator tooling** (`backend/scripts/workspace.py`) runs only on the server with database access, outside the
  served application (a test pins that `app/` never imports it). Passwords are never command-line arguments.
- **Upload (D-035, T2, T4, T11):**
  - **Authorization:** `policy:manage`, so STAFF get 403. Another tenant's policy is a 404, checked before
    anything is stored, and the composite foreign key refuses it anyway.
  - **No client path:** the client never names a path. Files are stored only under a server-generated reference,
    and the client's file name is display metadata (`safe_filename`).
  - **Validation:** the PDF is checked before storing (`unreadable_pdf`, `no_text`), and the response never
    carries parser text.
  - **Size:** request bodies are capped (`MAX_UPLOAD_BYTES`, 413) before any route or authentication runs.
  - **Known exception:** FastAPI still reads a body (now capped) before authentication.
- **Registration abuse:** anyone who can reach the API can register a company, and a 409 reveals that a company
  code exists. Rate limiting, CAPTCHA or approval are Phase 14.
- **Not yet:** rate limiting and lockout on sign-in and registration, refresh and revocation (Phase 14).

## 17. Security testing

The security metrics are defined in [EVALUATION.md](EVALUATION.md) §4: tenant leakage rate, unauthorized-access
failures, prompt-injection success rate, document-poisoning success rate, stale-answer rate and others.
The tests behind them are part of the golden dataset and run on every relevant change.

Since Phase -1, [EVALUATION.md](EVALUATION.md) §13 turns these into **gates**: every boundary in §2 that is built so
far has standing tests named there. A single failure, in those tests or in an evaluation run's tenant-leakage,
injection or citation checks, fails the run. Security is never averaged into a quality score.
