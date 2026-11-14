import { useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router";
import { isAbort } from "../../api/client";
import { api, chosenFilters } from "../../api/endpoints";
import {
  INSURANCE_TYPES,
  MAX_QUESTION_CHARS,
  type EvidenceHit,
  type InsuranceType,
  type RetrievalFilters,
  type SearchResponse,
} from "../../api/types";
import { EvidenceViewer, ProvenanceTrail } from "../../components/Evidence";
import { FormField } from "../../components/FormField";
import { INSURANCE_LABELS } from "../../components/format";
import { markTerms, queryTerms } from "../../components/highlight";
import { Icon } from "../../components/Icon";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { toApiError, useAsync } from "../../components/useAsync";
import { useDirectory, type Directory } from "../../components/useDirectory";
import type { ApiError } from "../../api/client";
import styles from "./SearchPage.module.css";

const RESULT_COUNTS = [5, 10, 20] as const;

function matchDescription(hit: EvidenceHit): string {
  const parts = [];
  if (hit.semantic_rank !== null) parts.push(`#${hit.semantic_rank} by meaning`);
  if (hit.keyword_rank !== null) parts.push(`#${hit.keyword_rank} by keywords`);
  return `Matched ${parts.join(" and ")}`;
}

/** Retrieval filters, shared with the question screen: only what the API supports, within the user's company. The
 * document filter lists the chosen policy's own versions in use. */
export function EvidenceFilters({ filters, onChange, disabled, withDocument = false }: {
  filters: RetrievalFilters;
  onChange: (filters: RetrievalFilters) => void;
  disabled?: boolean;
  withDocument?: boolean;
}) {
  const { state } = useAsync((signal) => api.listPolicies(signal), []);
  const policies = state.status === "ready" ? state.data.filter((p) => p.status === "ACTIVE") : [];
  const documents = useAsync(
    (signal) => (withDocument && filters.policy_id ? api.listDocuments(filters.policy_id, signal) : Promise.resolve([])),
    [withDocument, filters.policy_id],
  ).state;
  const versions = documents.status === "ready" ? documents.data.filter((d) => d.status === "UPLOADED") : [];
  return (
    <>
      <FormField label="Policy">
        <select className="select" value={filters.policy_id ?? ""} disabled={disabled}
          onChange={(event) => onChange({ ...filters, policy_id: event.target.value || undefined, document_id: undefined })}>
          <option value="">All active policies</option>
          {policies.map((policy) => (
            <option key={policy.id} value={policy.id}>
              {policy.policy_number} {policy.name}
            </option>
          ))}
        </select>
      </FormField>
      <FormField label="Insurance type">
        <select className="select" value={filters.insurance_type ?? ""} disabled={disabled}
          onChange={(event) => onChange({
            ...filters, insurance_type: (event.target.value || undefined) as InsuranceType | undefined,
          })}>
          <option value="">All types</option>
          {INSURANCE_TYPES.map((type) => (
            <option key={type} value={type}>{INSURANCE_LABELS[type]}</option>
          ))}
        </select>
      </FormField>
      {withDocument ? (
        <FormField label="Document">
          <select className="select" value={filters.document_id ?? ""} disabled={disabled || !filters.policy_id}
            onChange={(event) => onChange({ ...filters, document_id: event.target.value || undefined })}>
            <option value="">{filters.policy_id ? "All of its documents" : "Choose a policy first"}</option>
            {versions.map((document) => (
              <option key={document.id} value={document.id}>
                {document.original_filename}, version {document.version}
              </option>
            ))}
          </select>
        </FormField>
      ) : null}
    </>
  );
}

function EvidenceResult({ hit, rank, terms, directory }: {
  hit: EvidenceHit;
  rank: number;
  terms: string[];
  directory: Directory;
}) {
  return (
    <article className={styles.result} aria-label={`Result ${rank}`}>
      <div className={styles.resultHead}>
        <h3 className={styles.resultTitle}>{hit.section_heading ?? "Policy passage"}</h3>
        <span className={styles.rank}>Result {rank}</span>
      </div>
      <ProvenanceTrail source={hit} directory={directory} />
      <EvidenceViewer hit={hit} segments={markTerms(hit.chunk_text, terms)} />
      <span className={styles.match}>{matchDescription(hit)}</span>
    </article>
  );
}

export function SearchPage() {
  const [params] = useSearchParams();
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<RetrievalFilters>(() => ({
    policy_id: params.get("policy_id") || undefined,
    document_id: params.get("document_id") || undefined,
  }));
  const [count, setCount] = useState<number>(5);
  const [queryError, setQueryError] = useState<string | undefined>();
  const [result, setResult] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const directory = useDirectory(result ? result.hits.map((hit) => hit.document_id) : []);

  async function run(event?: FormEvent) {
    event?.preventDefault();
    const text = query.trim();
    if (!text) {
      setQueryError("Enter what you are looking for.");
      return;
    }
    setQueryError(undefined);
    controller.current?.abort();
    controller.current = new AbortController();
    setBusy(true);
    setError(null);
    try {
      setResult(await api.search(text, count, chosenFilters(filters), controller.current.signal));
    } catch (caught) {
      if (!isAbort(caught)) {
        const failure = toApiError(caught);
        if (failure.kind === "validation") setQueryError("Enter a search of at most 1,000 characters.");
        else setError(failure);
      }
    } finally {
      setBusy(false);
    }
  }

  const terms = result ? queryTerms(result.query) : [];
  return (
    <>
      <PageHeader
        title="Search evidence"
        description="Find the exact policy passages that support an answer. Results are quoted policy text, ranked by relevance; they are not answers."
      />
      <form className={styles.form} onSubmit={run} noValidate aria-busy={busy}>
        <div className={styles.queryRow}>
          <FormField label="Search for" error={queryError}>
            <input className={`input ${styles.queryInput}`} type="search" value={query} maxLength={MAX_QUESTION_CHARS}
              placeholder="for example: waiting period for maternity" onChange={(e) => setQuery(e.target.value)} />
          </FormField>
          <button type="submit" className="button button-primary" disabled={busy}>
            <Icon name="search" size={16} />
            {busy ? "Searching…" : "Search"}
          </button>
        </div>
        <div className={styles.filters}>
          <EvidenceFilters filters={filters} onChange={setFilters} disabled={busy} withDocument />
          <FormField label="Results">
            <select className="select" value={count} disabled={busy}
              onChange={(event) => setCount(Number(event.target.value))}>
              {RESULT_COUNTS.map((n) => <option key={n} value={n}>Top {n}</option>)}
            </select>
          </FormField>
        </div>
      </form>

      <section aria-live="polite" aria-busy={busy} className={styles.results}>
        {busy ? <LoadingState label="Searching policy evidence…" rows={4} showLabel /> : null}
        {!busy && error ? <ErrorState message={error.message} onRetry={() => run()} title="Search failed" /> : null}
        {!busy && !error && result && result.hits.length === 0 ? (
          <EmptyState title="No matching evidence" icon="search">
            Nothing in your company's active policy documents matched. Try different words, or broaden your filters.
            Only policies with indexed documents can be searched; see <Link to="/documents">Documents</Link>.
          </EmptyState>
        ) : null}
        {!busy && !error && result && result.hits.length > 0 ? (
          <>
            <h2 className={styles.resultsTitle}>
              {result.hits.length === 1 ? "1 passage" : `${result.hits.length} passages`} for “{result.query}”
            </h2>
            <ol className={styles.list}>
              {result.hits.map((hit, index) => (
                <li key={hit.chunk_id}>
                  <EvidenceResult hit={hit} rank={index + 1} terms={terms} directory={directory} />
                </li>
              ))}
            </ol>
          </>
        ) : null}
        {!busy && !error && !result ? (
          <p className={styles.hint}>
            To get an answer instead of passages, <Link to="/ask">ask a question</Link>.
          </p>
        ) : null}
      </section>
    </>
  );
}
