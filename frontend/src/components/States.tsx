import type { ReactNode } from "react";
import { Icon, type IconName } from "./Icon";
import styles from "./States.module.css";

/** A stable placeholder while data loads: the layout does not jump when it arrives. */
export function LoadingState({ label, rows = 3, showLabel = false }: { label: string; rows?: number; showLabel?: boolean }) {
  return (
    <div className={styles.loading} role="status" aria-live="polite" aria-busy="true">
      {showLabel ? <span className={styles.loadingLabel}>{label}</span> : <span className="visually-hidden">{label}</span>}
      {Array.from({ length: rows }, (_, index) => (
        <span key={index} className={styles.skeleton} style={{ width: `${92 - index * 14}%` }} />
      ))}
    </div>
  );
}

export function EmptyState({ title, children, action, icon = "document" }: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  icon?: IconName;
}) {
  return (
    <div className={styles.empty}>
      <Icon name={icon} size={28} className={styles.emptyIcon} />
      <h3 className={styles.emptyTitle}>{title}</h3>
      {children ? <p className={styles.emptyText}>{children}</p> : null}
      {action ? <div className={styles.emptyAction}>{action}</div> : null}
    </div>
  );
}

export function ErrorState({ message, onRetry, title = "This couldn't be loaded" }: {
  message: string;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <div className={styles.error} role="alert">
      <Icon name="conflict" size={22} className={styles.errorIcon} />
      <div className={styles.errorBody}>
        <p className={styles.errorTitle}>{title}</p>
        <p>{message}</p>
      </div>
      {onRetry ? (
        <button type="button" className="button button-secondary" onClick={onRetry}>
          <Icon name="retry" size={16} />
          Try again
        </button>
      ) : null}
    </div>
  );
}

/** An inline message after an action: what happened, in the interface's voice. */
export function Notice({ tone, children }: { tone: "error" | "success" | "info"; children: ReactNode }) {
  const icon: IconName = tone === "error" ? "conflict" : tone === "success" ? "answered" : "info";
  return (
    <div className={`${styles.notice} ${styles[tone]}`} role={tone === "error" ? "alert" : "status"}>
      <Icon name={icon} size={18} />
      <div>{children}</div>
    </div>
  );
}
