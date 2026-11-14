import { useState } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "../../api/endpoints";
import { INSURANCE_TYPES, type InsuranceType, type Policy, type PolicyCreate } from "../../api/types";
import { useSession } from "../../auth/AuthContext";
import { canManagePolicies } from "../../auth/permissions";
import { INSURANCE_LABELS, formatDate } from "../../components/format";
import { Icon } from "../../components/Icon";
import { PageHeader } from "../../components/PageHeader";
import { EmptyState, ErrorState, LoadingState, Notice } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import { useAsync } from "../../components/useAsync";
import { usePolicyDocuments } from "../../components/usePolicyDocuments";
import { PolicyForm } from "./PolicyForm";
import styles from "./Policies.module.css";

function PolicyTable({ policies, documentCounts }: {
  policies: Policy[];
  documentCounts: Map<string, number>;
}) {
  return (
    <div className="table-wrap">
      <table className="table">
        <caption className="visually-hidden">Policies</caption>
        <thead>
          <tr>
            <th scope="col">Policy number</th>
            <th scope="col">Name</th>
            <th scope="col">Insurance type</th>
            <th scope="col" className="num">Documents</th>
            <th scope="col">Status</th>
            <th scope="col">Last updated</th>
            <th scope="col"><span className="visually-hidden">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {policies.map((policy) => {
            const count = documentCounts.get(policy.id);
            return (
              <tr key={policy.id}>
                <td className={styles.code}>{policy.policy_number}</td>
                <td className={styles.strong}>{policy.name}</td>
                <td>{INSURANCE_LABELS[policy.insurance_type]}</td>
                <td className="num">{count === undefined ? "" : count}</td>
                <td><StatusBadge status={policy.status} /></td>
                <td className={styles.muted}>{formatDate(policy.updated_at)}</td>
                <td className="num">
                  <Link to={`/policies/${policy.id}`} className="button button-quiet"
                    aria-label={`Open policy ${policy.policy_number}`}>
                    Open
                    <Icon name="chevron" size={16} />
                  </Link>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PolicyList({ policies, manages }: { policies: Policy[]; manages: boolean }) {
  const [query, setQuery] = useState("");
  const [type, setType] = useState<InsuranceType | "ALL">("ALL");
  const documents = usePolicyDocuments(policies);
  const counts = new Map([...documents.byPolicy].map(([id, list]) => [id, list.length]));
  const needle = query.trim().toLowerCase();
  const shown = policies.filter((policy) =>
    (type === "ALL" || policy.insurance_type === type)
    && (!needle || `${policy.policy_number} ${policy.name}`.toLowerCase().includes(needle)));

  return (
    <div className={styles.listArea}>
      <div className={styles.toolbar}>
        <label className={styles.searchBox}>
          <Icon name="search" size={16} />
          <span className="visually-hidden">Search policies</span>
          <input className="input" type="search" value={query} placeholder="Search by number or name"
            onChange={(event) => setQuery(event.target.value)} />
        </label>
        <div className={styles.chips} role="group" aria-label="Insurance type">
          {(["ALL", ...INSURANCE_TYPES] as const).map((option) => (
            <button key={option} type="button" className={styles.chip} aria-pressed={type === option}
              onClick={() => setType(option)}>
              {option === "ALL" ? "All" : INSURANCE_LABELS[option]}
            </button>
          ))}
        </div>
      </div>
      {shown.length > 0 ? <PolicyTable policies={shown} documentCounts={counts} /> : (
        <EmptyState title="No policies match" icon="search"
          action={<button type="button" className="button button-secondary"
            onClick={() => { setQuery(""); setType("ALL"); }}>Clear filters</button>}>
          No policy matches {needle ? `“${query.trim()}”` : "this filter"}
          {type !== "ALL" ? ` among ${INSURANCE_LABELS[type].toLowerCase()} policies` : ""}.
        </EmptyState>
      )}
      {manages ? null : (
        <p className={styles.footnote}>Policies are created and changed by your company's administrators.</p>
      )}
    </div>
  );
}

export function PoliciesPage() {
  const session = useSession();
  const manages = canManagePolicies(session.user.role);
  const { state, reload } = useAsync((signal) => api.listPolicies(signal), []);

  return (
    <>
      <PageHeader
        title="Policies"
        description="The policies behind your evidence-grounded answers. Only active policies are searched and answered from."
        actions={manages ? (
          <Link to="/policies/new" className="button button-primary">
            <Icon name="plus" size={16} />
            New policy
          </Link>
        ) : null}
      />
      {state.status === "loading" ? <LoadingState label="Loading policies" rows={6} /> : null}
      {state.status === "error" ? <ErrorState message={state.error.message} onRetry={reload} /> : null}
      {state.status === "ready" && state.data.length === 0 ? (
        <EmptyState
          title="No policies yet"
          icon="policies"
          action={manages ? <Link className="button button-primary" to="/policies/new">New policy</Link> : null}
        >
          {manages
            ? "Create the first policy for your company. Then upload its PDF to make it searchable."
            : "A company admin adds your company's policies."}
        </EmptyState>
      ) : null}
      {state.status === "ready" && state.data.length > 0 ? <PolicyList policies={state.data} manages={manages} /> : null}
    </>
  );
}

export function NewPolicyPage() {
  const session = useSession();
  const navigate = useNavigate();

  if (!canManagePolicies(session.user.role)) {
    return (
      <>
        <PageHeader title="Create policy" back={<Link to="/policies">Back to policies</Link>} />
        <Notice tone="info">Only a company admin can create policies.</Notice>
      </>
    );
  }

  async function create(values: PolicyCreate) {
    const policy = await api.createPolicy(values);
    navigate(`/policies/${policy.id}`, { state: { created: true } });
  }

  return (
    <>
      <PageHeader
        title="Create policy"
        description="The policy starts as active. After creating it, upload its PDF on the policy's page to make it searchable."
        back={<Link to="/policies">Back to policies</Link>}
      />
      <div className={styles.formPanel}>
        <h2 className={styles.formTitle}>Policy details</h2>
        <PolicyForm submitLabel="Create policy" busyLabel="Creating…" onSubmit={create}
          onCancel={() => navigate("/policies")} />
      </div>
    </>
  );
}
