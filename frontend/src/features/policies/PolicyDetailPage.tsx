import { useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { api } from "../../api/endpoints";
import type { Policy, PolicyCreate, PolicyDocument } from "../../api/types";
import { useSession } from "../../auth/AuthContext";
import { canManagePolicies } from "../../auth/permissions";
import { INSURANCE_LABELS, formatBytes, formatDate } from "../../components/format";
import { Icon } from "../../components/Icon";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, LoadingState, Notice } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import { toApiError, useAsync } from "../../components/useAsync";
import { PolicyForm } from "./PolicyForm";
import { UploadPanel } from "./UploadPanel";
import styles from "./Policies.module.css";

function DocumentRow({ document, manages, onChanged, onDeleted }: {
  document: PolicyDocument;
  manages: boolean;
  onChanged: (document: PolicyDocument) => void;
  onDeleted: (id: string) => void;
}) {
  const [mode, setMode] = useState<"view" | "rename" | "confirm-delete">("view");
  const [name, setName] = useState(document.original_filename);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      setMode("view");
    } catch (caught) {
      setError(toApiError(caught).message);
    } finally {
      setBusy(false);
    }
  }

  function rename(event: FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a file name.");
      return;
    }
    void run(async () => onChanged(await api.renameDocument(document.id, trimmed)));
  }

  const nextStatus = document.status === "UPLOADED" ? "INACTIVE" : "UPLOADED";
  return (
    <>
      <tr>
        <td className={styles.strong}>
          <span className={styles.fileName}>
            <Icon name="document" size={16} />
            <Link to={`/documents/${document.id}`}>{document.original_filename}</Link>
          </span>
        </td>
        <td>{document.document_key}</td>
        <td className="num">{document.version}</td>
        <td><StatusBadge status={document.status} /></td>
        <td className="num">{formatBytes(document.size_bytes)}</td>
        <td>{formatDate(document.created_at)}</td>
        {manages ? (
          <td className={styles.rowActions}>
            <button type="button" className="button button-quiet" disabled={busy}
              onClick={() => { setMode(mode === "rename" ? "view" : "rename"); setError(null); }}>
              Rename<span className="visually-hidden"> {document.original_filename}</span>
            </button>
            <button type="button" className="button button-quiet" disabled={busy}
              onClick={() => run(async () => onChanged(await api.setDocumentStatus(document.id, nextStatus)))}>
              {nextStatus === "INACTIVE" ? "Withdraw" : "Use again"}
              <span className="visually-hidden"> {document.original_filename}</span>
            </button>
            <button type="button" className="button button-quiet" disabled={busy}
              onClick={() => { setMode("confirm-delete"); setError(null); }}>
              Delete<span className="visually-hidden"> {document.original_filename}</span>
            </button>
          </td>
        ) : null}
      </tr>
      {mode !== "view" || error ? (
        <tr className={styles.subRow}>
          <td colSpan={manages ? 7 : 6}>
            {mode === "rename" ? (
              <form className={styles.inlineForm} onSubmit={rename}>
                <label className="visually-hidden" htmlFor={`rename-${document.id}`}>New file name</label>
                <input id={`rename-${document.id}`} className="input" value={name} maxLength={255} disabled={busy}
                  onChange={(event) => setName(event.target.value)} />
                <button type="submit" className="button button-primary" disabled={busy}>
                  {busy ? "Saving…" : "Save name"}
                </button>
                <button type="button" className="button button-secondary" disabled={busy}
                  onClick={() => { setMode("view"); setName(document.original_filename); setError(null); }}>
                  Cancel
                </button>
              </form>
            ) : null}
            {mode === "confirm-delete" ? (
              <div className={styles.inlineForm} role="group" aria-label="Confirm deletion">
                <p>Delete version {document.version} of {document.original_filename}? This can't be undone.</p>
                <button type="button" className="button button-danger" disabled={busy}
                  onClick={() => run(async () => { await api.deleteDocument(document.id); onDeleted(document.id); })}>
                  {busy ? "Deleting…" : "Delete version"}
                </button>
                <button type="button" className="button button-secondary" disabled={busy}
                  onClick={() => { setMode("view"); setError(null); }}>
                  Keep it
                </button>
              </div>
            ) : null}
            {error ? <Notice tone="error">{error}</Notice> : null}
          </td>
        </tr>
      ) : null}
    </>
  );
}

function Documents({ policyId, manages }: { policyId: string; manages: boolean }) {
  const { state, reload, setState } = useAsync((signal) => api.listDocuments(policyId, signal), [policyId]);
  const documents = state.status === "ready" ? state.data : [];
  const replace = (next: PolicyDocument[]) => setState({ status: "ready", data: next });

  return (
    <section aria-labelledby="documents-title" className={styles.section}>
      <div className={styles.sectionHead}>
        <h2 id="documents-title">Documents</h2>
        <p className={styles.muted}>
          Each row is one version of a policy document. Versions in use are searched and answered from; withdrawn
          versions are kept for the record.
        </p>
      </div>
      {state.status === "loading" ? <LoadingState label="Loading documents" /> : null}
      {state.status === "error" ? <ErrorState message={state.error.message} onRetry={reload} /> : null}
      {state.status === "ready" && documents.length === 0 ? (
        <EmptyState title="No documents yet">
          {manages
            ? "Upload the policy's PDF wording below. Once it is indexed, it can be searched and answered from."
            : "This policy has no documents yet, so it can't be searched or answered from. A company admin uploads them."}
        </EmptyState>
      ) : null}
      {state.status === "ready" && documents.length > 0 ? (
        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">Documents of this policy</caption>
            <thead>
              <tr>
                <th scope="col">File</th>
                <th scope="col">Document</th>
                <th scope="col" className="num">Version</th>
                <th scope="col">Status</th>
                <th scope="col" className="num">Size</th>
                <th scope="col">Added</th>
                {manages ? <th scope="col"><span className="visually-hidden">Actions</span></th> : null}
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => (
                <DocumentRow key={document.id} document={document} manages={manages}
                  onChanged={(changed) => replace(documents.map((d) => (d.id === changed.id ? changed : d)))}
                  onDeleted={(id) => replace(documents.filter((d) => d.id !== id))} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {manages && state.status === "ready" ? (
        <UploadPanel policyId={policyId} documents={documents}
          onUploaded={(uploaded) => replace([...documents.filter((d) => d.id !== uploaded.id), uploaded])} />
      ) : null}
    </section>
  );
}

function PolicyView({ initial, manages }: { initial: Policy; manages: boolean }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [policy, setPolicy] = useState(initial);
  const [mode, setMode] = useState<"view" | "edit" | "confirm-delete">("view");
  const [notice, setNotice] = useState<string | null>(
    (location.state as { created?: boolean } | null)?.created ? "Policy created." : null,
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function save(values: PolicyCreate) {
    const updated = await api.updatePolicy(policy.id, values);
    setPolicy(updated);
    setMode("view");
    setNotice("Policy details saved.");
  }

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.deletePolicy(policy.id);
      navigate("/policies", { replace: true });
    } catch (caught) {
      setError(toApiError(caught).message);
      setBusy(false);
    }
  }

  return (
    <>
      <PageHeader
        title={`${policy.policy_number} ${policy.name}`}
        back={<Link to="/policies">Back to policies</Link>}
        actions={manages && mode === "view" ? (
          <>
            <button type="button" className="button button-secondary"
              onClick={() => { setMode("edit"); setNotice(null); }}>
              <Icon name="edit" size={16} />
              Edit details
            </button>
            <button type="button" className="button button-secondary"
              onClick={() => { setMode("confirm-delete"); setNotice(null); setError(null); }}>
              <Icon name="trash" size={16} />
              Delete policy
            </button>
          </>
        ) : null}
      />
      {notice ? <Notice tone="success">{notice}</Notice> : null}

      {mode === "edit" ? (
        <div className={styles.formPanel}>
          <PolicyForm initial={policy} submitLabel="Save details" busyLabel="Saving…" onSubmit={save}
            onCancel={() => setMode("view")} />
        </div>
      ) : (
        <dl className={styles.facts}>
          <div><dt>Insurance type</dt><dd>{INSURANCE_LABELS[policy.insurance_type]}</dd></div>
          <div><dt>Status</dt><dd><StatusBadge status={policy.status} /></dd></div>
          <div><dt>Created</dt><dd>{formatDate(policy.created_at)}</dd></div>
          <div><dt>Last updated</dt><dd>{formatDate(policy.updated_at)}</dd></div>
        </dl>
      )}

      {mode === "confirm-delete" ? (
        <div className={styles.confirm} role="group" aria-label="Confirm policy deletion">
          <p>
            Delete policy {policy.policy_number}? This can't be undone. A policy that still has documents can't be
            deleted.
          </p>
          <div className={styles.formActions}>
            <button type="button" className="button button-danger" onClick={remove} disabled={busy}>
              {busy ? "Deleting…" : "Delete policy"}
            </button>
            <button type="button" className="button button-secondary" onClick={() => setMode("view")} disabled={busy}>
              Keep policy
            </button>
          </div>
          {error ? <Notice tone="error">{error}</Notice> : null}
        </div>
      ) : null}

      <Documents policyId={policy.id} manages={manages} />
    </>
  );
}

export function PolicyDetailPage() {
  const { policyId = "" } = useParams();
  const session = useSession();
  const { state, reload } = useAsync((signal) => api.getPolicy(policyId, signal), [policyId]);

  if (state.status === "loading") return <LoadingState label="Loading policy" rows={4} />;
  if (state.status === "error") {
    return (
      <>
        <PageHeader title="Policy" back={<Link to="/policies">Back to policies</Link>} />
        <ErrorState title={state.error.kind === "not_found" ? "Policy not found" : "This couldn't be loaded"}
          message={state.error.message} onRetry={state.error.kind === "not_found" ? undefined : reload} />
      </>
    );
  }
  return <PolicyView key={state.data.id} initial={state.data} manages={canManagePolicies(session.user.role)} />;
}
