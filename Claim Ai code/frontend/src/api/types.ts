// The ClaimsRAG API contract, as the backend serves it (app/api/*.py). The client never adds tenant, role or
// permission fields: the backend takes them from the verified token.

export type Role = "COMPANY_ADMIN" | "STAFF";
export type InsuranceType = "HEALTH" | "MOTOR" | "TRAVEL" | "HOME";
export type PolicyStatus = "ACTIVE" | "INACTIVE";
export type DocumentStatus = "UPLOADED" | "INACTIVE";
export type AnswerStatus = "ANSWERED" | "CONFLICT" | "ABSTAINED";

export const INSURANCE_TYPES: readonly InsuranceType[] = ["HEALTH", "MOTOR", "TRAVEL", "HOME"];

export interface SignedInUser {
  full_name: string;
  email: string;
  role: Role;
}

export interface DemoSessionResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: SignedInUser;
  company: { name: string; slug: string };
}

export interface Policy {
  id: string;
  policy_number: string;
  name: string;
  insurance_type: InsuranceType;
  status: PolicyStatus;
  created_at: string;
  updated_at: string;
}

export interface PolicyCreate {
  policy_number: string;
  name: string;
  insurance_type: InsuranceType;
  status?: PolicyStatus;
}

export type PolicyUpdate = Partial<Pick<PolicyCreate, "policy_number" | "name" | "insurance_type">>;

export interface PolicyDocument {
  id: string;
  policy_id: string;
  document_key: string;
  version: number;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  checksum_sha256: string;
  status: DocumentStatus;
  created_at: string;
  updated_at: string;
}

export interface DocumentUploadResponse {
  document: PolicyDocument;
  page_count: number;
  chunk_count: number;
}

export interface RetrievalFilters {
  policy_id?: string;
  document_id?: string;
  insurance_type?: InsuranceType;
}

export interface EvidenceHit {
  chunk_id: string;
  document_id: string;
  policy_id: string;
  document_version: number;
  chunk_index: number;
  page_start: number;
  page_end: number;
  section_heading: string | null;
  chunk_text: string;
  semantic_rank: number | null;
  semantic_score: number | null;
  keyword_rank: number | null;
  keyword_score: number | null;
  hybrid_score: number;
}

export interface SearchResponse {
  query: string;
  filters: RetrievalFilters;
  top_k: number;
  method: "hybrid";
  embedding_model: string;
  hits: EvidenceHit[];
}

export interface Citation {
  chunk_id: string;
  document_id: string;
  policy_id: string;
  document_version: number;
  page_start: number;
  page_end: number;
  section_heading: string | null;
}

export interface ChatResponse {
  question: string;
  status: AnswerStatus;
  answer: string;
  citations: Citation[];
  model: string;
  embedding_model: string;
  generated: boolean;
  evidence_ids: string[];
  evidence: EvidenceHit[];
  retrieval_ms: number;
  generation_ms: number;
  output_tokens: number | null;
}

// Limits the backend enforces (app/retrieval.py, app/api/chat.py), mirrored only to shape the controls.
export const MAX_QUESTION_CHARS = 1000;
export const MAX_SEARCH_RESULTS = 50;
export const MAX_CHAT_PASSAGES = 8;
// The backend's default MAX_UPLOAD_BYTES (25 MiB), mirrored for usability only.
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;
