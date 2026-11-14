import { Link } from "react-router";
import { api } from "../../api/endpoints";
import { INSURANCE_TYPES, type Policy } from "../../api/types";
import { useSession } from "../../auth/AuthContext";
import { canManagePolicies, ROLE_LABELS } from "../../auth/permissions";
import { INSURANCE_LABELS, formatDate } from "../../components/format";
import { Icon, type IconName } from "../../components/Icon";
import { EmptyState, ErrorState, LoadingState } from "../../components/States";
import { StatusBadge } from "../../components/StatusBadge";
import { useAsync } from "../../components/useAsync";
import { usePolicyDocuments } from "../../components/usePolicyDocuments";
import styles from "./DashboardPage.module.css";

function greeting(now = new Date()): string {
  const hour = now.getHours();
  return hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
}

function Stat({ value, label, detail }: { value: string; label: string; detail?: string }) {
  return (
    <div className={styles.stat}>
      <span className={styles.statValue}>{value}</span>
      <span className={styles.statLabel}>{label}</span>
      {detail ? <span className={styles.statDetail}>{detail}</span> : null}
    </div>
  );
}

function Workspace({ policies }: { policies: Policy[] }) {
  const documents = usePolicyDocuments(policies);
  const active = policies.filter((p) => p.status === "ACTIVE");
  const inUse = [...documents.byPolicy.values()].flat().filter((d) => d.status === "UPLOADED").length;
  const incomplete = documents.byPolicy.size < policies.length;
  const types = INSURANCE_TYPES.filter((type) => active.some((p) => p.insurance_type === type));
  const recent = [...policies].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 6);

  return (
    <>
      <section className={styles.stats} aria-label="Workspace at a glance">
        <Stat value={String(active.length)} label="Active policies"
          detail={policies.length > active.length ? `${policies.length - active.length} inactive` : undefined} />
        <Stat value={documents.loading ? "…" : incomplete ? "Not available" : String(inUse)} label="Documents in use"
          detail="Searched and answered from" />
        <Stat value={`${types.length} of ${INSURANCE_TYPES.length}`} label="Insurance types covered"
          detail={types.map((type) => INSURANCE_LABELS[type]).join(", ") || undefined} />
      </section>

      <section aria-labelledby="recent" className={styles.recentSection}>
        <div className={styles.sectionHead}>
          <h2 id="recent" className={styles.sectionTitle}>Recently updated policies</h2>
          <Link to="/policies" className={styles.sectionLink}>All policies</Link>
        </div>
        <ul className={styles.recent}>
          {recent.map((policy) => {
            const count = documents.byPolicy.get(policy.id)?.length;
            return (
              <li key={policy.id}>
                <Link to={`/policies/${policy.id}`} className={styles.recentLink}>
                  <span className={styles.recentName}>
                    <span className={styles.number}>{policy.policy_number}</span>
                    <span>{policy.name}</span>
                  </span>
                  <span className={styles.recentMeta}>
                    <span className={styles.type}>{INSURANCE_LABELS[policy.insurance_type]}</span>
                    <span>{count === undefined ? "" : count === 1 ? "1 document" : `${count} documents`}</span>
                    <StatusBadge status={policy.status} />
                    <span className={styles.date}>{formatDate(policy.updated_at)}</span>
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      </section>
    </>
  );
}

export function DashboardPage() {
  const session = useSession();
  const manages = canManagePolicies(session.user.role);
  const { state, reload } = useAsync((signal) => api.listPolicies(signal), []);
  const firstName = session.user.full_name.split(/\s+/)[0] ?? session.user.full_name;
  const actions: { to: string; icon: IconName; title: string; text: string }[] = [
    ...(manages ? [
      { to: "/policies/new", icon: "plus" as IconName, title: "Create a policy", text: "Add a policy to your evidence base." },
      { to: "/policies", icon: "upload" as IconName, title: "Upload a policy PDF", text: "Open a policy, then add its wording." },
    ] : [
      { to: "/policies", icon: "policies" as IconName, title: "Browse policies", text: "Your company's policies and documents." },
    ]),
    { to: "/search", icon: "search", title: "Search evidence", text: "Find the exact passages on a benefit or limit." },
    { to: "/ask", icon: "ask", title: "Ask a question", text: "Get a grounded answer with its page citations." },
  ];

  return (
    <>
      <header className={styles.header}>
        <h1>{greeting()}, {firstName}</h1>
        <p className={styles.subtitle}>
          Your policy intelligence workspace for {session.company.name}, working as{" "}
          {ROLE_LABELS[session.user.role].toLowerCase()}.
        </p>
      </header>

      <nav className={styles.actions} aria-label="Start a task">
        {actions.map((action) => (
          <Link key={action.title} to={action.to} className={styles.action}>
            <Icon name={action.icon} size={18} />
            <span>
              <span className={styles.actionTitle}>{action.title}</span>
              <span className={styles.actionText}>{action.text}</span>
            </span>
          </Link>
        ))}
      </nav>

      {state.status === "loading" ? <LoadingState label="Loading your workspace" rows={5} /> : null}
      {state.status === "error" ? <ErrorState message={state.error.message} onRetry={reload} /> : null}
      {state.status === "ready" && state.data.length === 0 ? (
        <EmptyState
          title="No policies yet"
          icon="policies"
          action={manages ? <Link className="button button-primary" to="/policies/new">Create policy</Link> : null}
        >
          {manages
            ? "Create your first policy to start building your company's evidence base."
            : "A company admin adds policies. Once a policy's wording is indexed, you can search it and ask about it."}
        </EmptyState>
      ) : null}
      {state.status === "ready" && state.data.length > 0 ? <Workspace policies={state.data} /> : null}
    </>
  );
}
