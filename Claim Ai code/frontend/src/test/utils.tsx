import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { expect, vi } from "vitest";
import type {
  ChatResponse, DemoSessionResponse, EvidenceHit, Policy, PolicyDocument, Role, SearchResponse, SignedInUser,
} from "../api/types";
import { App } from "../App";
import { SessionProvider } from "../auth/AuthContext";

export interface RecordedRequest {
  method: string;
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

type Reply = { status: number; body?: unknown };
type Route = Reply | ((request: RecordedRequest) => Reply | Promise<Reply>);

export const TOKEN = "header.payload-of-test-token.signature";
const DEMO_SESSION = "POST /auth/demo-session";

let demo: DemoSessionResponse = demoSessionFor("COMPANY_ADMIN");

function demoSessionFor(role: Role, user: Partial<SignedInUser> = {}): DemoSessionResponse {
  return {
    access_token: TOKEN,
    token_type: "bearer",
    expires_in: 15 * 60,
    user: { full_name: role === "STAFF" ? "Rahul Nair" : "Asha Menon", email: "user@demo.example", role, ...user },
    company: { name: "Demo Health Insurance (sample)", slug: "demo-health" },
  };
}

/** Who the server's demo session opens the workspace as (an admin unless a test says otherwise). */
export function demoUser(role: Role = "COMPANY_ADMIN", user: Partial<SignedInUser> = {}): DemoSessionResponse {
  demo = demoSessionFor(role, user);
  return demo;
}

/** Called after each test (test/setup.ts). */
export function resetDemoUser(): void {
  demo = demoSessionFor("COMPANY_ADMIN");
}

/** Replace fetch with a route table keyed "METHOD /path" (paths without the /api prefix). Unknown routes are 404.
 * "POST /auth/demo-session" answers with the demoUser() session unless the table says otherwise. Its requests are
 * recorded in sessionRequests, so requests holds only what the page itself asked for. */
export function mockApi(routes: Record<string, Route>) {
  const requests: RecordedRequest[] = [];
  const sessionRequests: RecordedRequest[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const request: RecordedRequest = {
      method: init?.method ?? "GET",
      path: url.pathname.replace(/^\/api/, ""),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      headers: { ...(init?.headers as Record<string, string>) },
    };
    const key = `${request.method} ${request.path}`;
    (key === DEMO_SESSION ? sessionRequests : requests).push(request);
    const route = routes[key] ?? (key === DEMO_SESSION ? { status: 200, body: demo } : undefined);
    const reply = typeof route === "function" ? await route(request) : (route ?? { status: 404, body: { detail: "Not Found" } });
    if (init?.signal?.aborted) throw new DOMException("Aborted", "AbortError");
    return new Response(reply.body === undefined ? null : JSON.stringify(reply.body), {
      status: reply.status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { requests, sessionRequests, fetchMock };
}

/** Render the app at a path, without waiting for anything. */
export function renderAppNow(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SessionProvider>
        <App />
      </SessionProvider>
    </MemoryRouter>,
  );
}

/** Render the app at a path, once its workspace session has opened (or failed to). */
export async function renderApp(path = "/") {
  const result = renderAppNow(path);
  await waitFor(() => expect(screen.queryByRole("status", { name: "Opening your workspace" })).toBeNull());
  return result;
}

const POLICY_ID = "11111111-1111-4111-8111-111111111111";
const DOCUMENT_ID = "22222222-2222-4222-8222-222222222222";

export const policy: Policy = {
  id: POLICY_ID,
  policy_number: "DH-100",
  name: "Family Health Plus",
  insurance_type: "HEALTH",
  status: "ACTIVE",
  created_at: "2026-11-01T10:00:00Z",
  updated_at: "2026-11-02T10:00:00Z",
};

export const inactivePolicy: Policy = {
  ...policy,
  id: "33333333-3333-4333-8333-333333333333",
  policy_number: "MT-7",
  name: "Motor Secure",
  insurance_type: "MOTOR",
  status: "INACTIVE",
};

export const policyDocument: PolicyDocument = {
  id: DOCUMENT_ID,
  policy_id: POLICY_ID,
  document_key: "policy-wording",
  version: 2,
  original_filename: "family-health-wording.pdf",
  content_type: "application/pdf",
  size_bytes: 5734,
  checksum_sha256: "a".repeat(64),
  status: "UPLOADED",
  created_at: "2026-11-02T10:00:00Z",
  updated_at: "2026-11-02T10:00:00Z",
};

export function hit(index: number, text: string, overrides: Partial<EvidenceHit> = {}): EvidenceHit {
  return {
    chunk_id: `00000000-0000-4000-8000-00000000000${index}`,
    document_id: DOCUMENT_ID,
    policy_id: POLICY_ID,
    document_version: 2,
    chunk_index: index,
    page_start: index + 2,
    page_end: index + 2,
    section_heading: `SECTION ${index}`,
    chunk_text: text,
    semantic_rank: index,
    semantic_score: 0.8,
    keyword_rank: index === 1 ? 1 : null,
    keyword_score: index === 1 ? 0.3 : null,
    hybrid_score: 0.03,
    ...overrides,
  };
}

export const MATERNITY = "Maternity expenses are covered after a waiting period of 9 months.";
export const AMBULANCE = "Road ambulance charges are reimbursed up to Rs 2,000 per hospitalisation.";

function citationOf(evidence: EvidenceHit) {
  const { chunk_id, document_id, policy_id, document_version, page_start, page_end, section_heading } = evidence;
  return { chunk_id, document_id, policy_id, document_version, page_start, page_end, section_heading };
}

export function chatResponse(status: ChatResponse["status"], overrides: Partial<ChatResponse> = {}): ChatResponse {
  const evidence = [hit(1, MATERNITY), hit(2, AMBULANCE)];
  const base: ChatResponse = {
    question: "What is the maternity waiting period?",
    status,
    answer: "Maternity expenses are covered after 9 months [1].",
    citations: [citationOf(evidence[0]!)],
    model: "qwen2.5:3b",
    embedding_model: "Qdrant/bge-small-en-v1.5-onnx-Q",
    generated: true,
    evidence_ids: evidence.map((e) => e.chunk_id),
    evidence,
    retrieval_ms: 120,
    generation_ms: 4300,
    output_tokens: 40,
  };
  if (status === "CONFLICT") {
    base.answer = "The available policy evidence contains conflicting information. [1] says 9 months while [2] says 12 months.";
    base.citations = evidence.map(citationOf);
  }
  if (status === "ABSTAINED") {
    base.answer = "The provided policy evidence is insufficient to determine the answer.";
    base.citations = [];
  }
  return { ...base, ...overrides };
}

export function searchResponse(hits: EvidenceHit[]): SearchResponse {
  return { query: "maternity waiting period", filters: {}, top_k: 5, method: "hybrid", embedding_model: "test", hits };
}
