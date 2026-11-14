import type { AnswerStatus, DocumentStatus, PolicyStatus } from "../api/types";
import { Icon, type IconName } from "./Icon";
import styles from "./StatusBadge.module.css";

type Tone = "answered" | "conflict" | "abstained" | "neutral";

const BADGES: Record<PolicyStatus | DocumentStatus | AnswerStatus, { label: string; tone: Tone; icon?: IconName }> = {
  ACTIVE: { label: "Active", tone: "answered" },
  UPLOADED: { label: "In use", tone: "answered" },
  INACTIVE: { label: "Inactive", tone: "neutral" },
  ANSWERED: { label: "Answered", tone: "answered", icon: "answered" },
  CONFLICT: { label: "Conflicting evidence", tone: "conflict", icon: "conflict" },
  ABSTAINED: { label: "Not enough evidence", tone: "abstained", icon: "abstained" },
};

/** A status as text with its colour (and an icon for answers): never colour alone. */
export function StatusBadge({ status }: { status: PolicyStatus | DocumentStatus | AnswerStatus }) {
  const badge = BADGES[status];
  return (
    <span className={`${styles.badge} ${styles[badge.tone]}`}>
      {badge.icon ? <Icon name={badge.icon} size={14} /> : <span className={styles.dot} aria-hidden="true" />}
      {badge.label}
    </span>
  );
}
