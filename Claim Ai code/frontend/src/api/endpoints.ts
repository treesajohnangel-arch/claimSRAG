import { request, uploadFile } from "./client";
import type {
  ChatResponse,
  DemoSessionResponse,
  DocumentUploadResponse,
  DocumentStatus,
  Policy,
  PolicyCreate,
  PolicyDocument,
  PolicyUpdate,
  RetrievalFilters,
  SearchResponse,
} from "./types";

const id = (value: string) => encodeURIComponent(value);

export const api = {
  /** The single-company demo session (D-036): no body, no token. The server decides the user and company. */
  demoSession: () => request<DemoSessionResponse>("/auth/demo-session", { method: "POST", anonymous: true }),

  listPolicies: (signal?: AbortSignal) => request<Policy[]>("/policies", { signal }),
  getPolicy: (policyId: string, signal?: AbortSignal) => request<Policy>(`/policies/${id(policyId)}`, { signal }),
  createPolicy: (body: PolicyCreate) => request<Policy>("/policies", { method: "POST", body }),
  updatePolicy: (policyId: string, body: PolicyUpdate) =>
    request<Policy>(`/policies/${id(policyId)}`, { method: "PATCH", body }),
  deletePolicy: (policyId: string) => request<void>(`/policies/${id(policyId)}`, { method: "DELETE" }),

  listDocuments: (policyId: string, signal?: AbortSignal) =>
    request<PolicyDocument[]>(`/policies/${id(policyId)}/documents`, { signal }),
  getDocument: (documentId: string, signal?: AbortSignal) =>
    request<PolicyDocument>(`/documents/${id(documentId)}`, { signal }),
  renameDocument: (documentId: string, originalFilename: string) =>
    request<PolicyDocument>(`/documents/${id(documentId)}`, {
      method: "PATCH",
      body: { original_filename: originalFilename },
    }),
  setDocumentStatus: (documentId: string, status: DocumentStatus) =>
    request<PolicyDocument>(`/documents/${id(documentId)}/status`, { method: "PUT", body: { status } }),
  deleteDocument: (documentId: string) => request<void>(`/documents/${id(documentId)}`, { method: "DELETE" }),
  uploadDocument: (policyId: string, file: File, documentKey: string, version: number,
    onProgress: (stage: "uploading" | "processing", fraction: number) => void) => {
    const query = new URLSearchParams({ filename: file.name, document_key: documentKey, version: String(version) });
    return uploadFile<DocumentUploadResponse>(`/policies/${id(policyId)}/documents/upload?${query}`, file,
      "application/pdf", onProgress);
  },

  search: (query: string, topK: number, filters: RetrievalFilters, signal?: AbortSignal) =>
    request<SearchResponse>("/search", { method: "POST", body: { query, top_k: topK, filters }, signal }),
  chat: (question: string, topK: number, filters: RetrievalFilters, signal?: AbortSignal) =>
    request<ChatResponse>("/chat", { method: "POST", body: { question, top_k: topK, filters }, signal }),
};

/** Only the filters the user actually chose: an empty choice is left out, never sent as a blank value. */
export function chosenFilters(filters: RetrievalFilters): RetrievalFilters {
  const chosen: RetrievalFilters = {};
  if (filters.policy_id) chosen.policy_id = filters.policy_id;
  if (filters.document_id) chosen.document_id = filters.document_id;
  if (filters.insurance_type) chosen.insurance_type = filters.insurance_type;
  return chosen;
}
