import { Link, useParams } from "react-router";
import { api } from "../../api/endpoints";
import type { Policy, PolicyDocument } from "../../api/types";
import { useSession } from "../../auth/AuthContext";
import { canManagePolicies } from "../../auth/permissions";
import { INSURANCE_LABELS, formatBytes, formatDate } from "../../components/format";
import { Icon } from "../../components/Icon";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, LoadingState, Notice } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import { useAsync } from "../../components/useAsync";
import { usePolicyDocuments } from "../../components/usePolicyDocuments";
import styles from "./Documents.module.css";

function AllDocuments({ policies }: { policies: Policy[] }) {
  const session = useSession();
  const documents = usePolicyDocuments(policies);
  const byId = new Map(policies.map((policy) => [policy.id, policy]));
  const rows = [...documents.byPolicy.values()].flat().sort((a, b) => b.created_at.localeCompare(a.created_at));
  const missing = policies.length - documents.byPolicy.size;

  if (documents.loading) return <LoadingState label="Loading documents" rows={5} />;
  return (
    <div className={styles.area}>
      {missing > 0 ? <Notice tone="info">The documents of {missing} {missing === 1 ? "policy" : "policies"} couldn't be loaded.</Notice> : null}
      {rows.length === 0 ? (
        <EmptyState title="No documents yet" icon="documents"
          action={<Link className="button button-primary" to="/policies">Go to policies</Link>}>
          {canManagePolicies(session.user.role)
            ? "Upload a policy PDF on its policy's page to make it searchable."
            : "A company admin uploads policy PDFs. Once indexed, they can be searched and asked about."}
        </EmptyState>
      ) : (
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">Documents</caption>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Policy</th>
                <th scope="col" className="num">Version</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">Size</th>
                <th scope="col">Added</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((document) => {
                const policy = byId.get(document.policy_id);
                return (
                  <tr key={document.id}>
                    <td className={styles.file}>
                      <Icon name="document" size={16} />
                      <Link to={`/documents/${document.id}`}>{document.original_filename}</Link>
                    </td>
                    <td>
                      {policy ? (
                        <Link to={`/policies/${policy.id}`} className={styles.policyLink}>
                          <span className="mono">{policy.policy_number}</span> {policy.name}
                        </Link>
                      ) : null}
                    </td>
                    <td className="num">{document.version}</td>
                    <td><StatusBadge status={document.status} /></td>
                    <td className="num">{formatBytes(document.size_bytes)}</td>
                    <td className={styles.muted}>{formatDate(document.created_at)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export function DocumentsPage() {
  const { state, reload } = useAsync((signal) => api.listPolicies(signal), []);
  return (
    <>
      <PageHeader
        title="Documents"
        description="Every policy document version in your workspace. Versions in use are what search and answers read; withdrawn versions are kept for the record."
      />
      {state.status === "loading" ? <LoadingState label="Loading documents" rows={5} /> : null}
      {state.status === "error" ? <ErrorState message={state.error.message} onRetry={reload} /> : null}
      {state.status === "ready" ? <AllDocuments policies={state.data} /> : null}
    </>
  );
}

function DocumentView({ document }: { document: PolicyDocument }) {
  const { state } = useAsync((signal) => api.getPolicy(document.policy_id, signal), [document.policy_id]);
  const policy = state.status === "ready" ? state.data : null;
  const search = `/search?policy_id=${document.policy_id}&document_id=${document.id}`;
  return (
    <>
      <PageHeader
        title={document.original_filename}
        description="A policy document is the source of truth: every passage ClaimsRAG retrieves from it, and every answer that cites it, names this file, its version and the page."
        back={<Link to={policy ? `/policies/${policy.id}` : "/documents"}>{policy ? `Back to ${policy.policy_number}` : "Back to documents"}</Link>}
        actions={(
          <>
            <Link to={search} className="button button-secondary">
              <Icon name="search" size={16} />
              Search this document
            </Link>
            <Link to={`/ask?policy_id=${document.policy_id}`} className="button button-secondary">
              <Icon name="ask" size={16} />
              Ask about this policy
            </Link>
          </>
        )}
      />
      <dl className={styles.facts}>
        <div>
          <dt>Policy</dt>
          <dd>{policy ? <Link to={`/policies/${policy.id}`}>{policy.policy_number} {policy.name}</Link> : "…"}</dd>
        </div>
        {policy ? <div><dt>Insurance type</dt><dd>{INSURANCE_LABELS[policy.insurance_type]}</dd></div> : null}
        <div><dt>Document</dt><dd className="mono">{document.document_key}</dd></div>
        <div><dt>Version</dt><dd className="mono">{document.version}</dd></div>
        <div><dt>Status</dt><dd><StatusBadge status={document.status} /></dd></div>
        <div><dt>Size</dt><dd>{formatBytes(document.size_bytes)}</dd></div>
        <div><dt>Uploaded</dt><dd>{formatDate(document.created_at)}</dd></div>
        <div className={styles.wide}>
          <dt>Fingerprint (SHA-256)</dt>
          <dd className={`mono ${styles.checksum}`} title={document.checksum_sha256}>{document.checksum_sha256}</dd>
        </div>
      </dl>

      <section className={styles.structure} aria-labelledby="structure-title">
        <h2 id="structure-title" className={styles.sectionTitle}>Pages and passages</h2>
        <p className={styles.muted}>
          {document.status === "UPLOADED"
            ? "This version is in use. Its text was extracted page by page and split into passages; each passage keeps its page and section, which is what citations point to."
            : "This version is withdrawn: it is kept for the record, but search and answers no longer read it."}
          {" "}A page-by-page outline isn't available here yet. To inspect the passages, search within this document:
          each result shows its page and section.
        </p>
        {document.status === "UPLOADED" ? (
          <Link to={search} className="button button-primary">
            <Icon name="search" size={16} />
            Search this document
          </Link>
        ) : null}
      </section>
    </>
  );
}

export function DocumentDetailPage() {
  const { documentId = "" } = useParams();
  const { state, reload } = useAsync((signal) => api.getDocument(documentId, signal), [documentId]);
  if (state.status === "loading") return <LoadingState label="Loading document" rows={4} />;
  if (state.status === "error") {
    return (
      <>
        <PageHeader title="Document" back={<Link to="/documents">Back to documents</Link>} />
        <ErrorState title={state.error.kind === "not_found" ? "Document not found" : "This couldn't be loaded"}
          message={state.error.message} onRetry={state.error.kind === "not_found" ? undefined : reload} />
      </>
    );
  }
  return <DocumentView document={state.data} />;
}
