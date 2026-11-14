import type { ReactNode } from "react";
import styles from "./PageHeader.module.css";

export function PageHeader({ title, description, actions, back }: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  back?: ReactNode;
}) {
  return (
    <header className={styles.header}>
      {back ? <div className={styles.back}>{back}</div> : null}
      <div className={styles.row}>
        <div className={styles.text}>
          <h1>{title}</h1>
          {description ? <p className={styles.description}>{description}</p> : null}
        </div>
        {actions ? <div className={styles.actions}>{actions}</div> : null}
      </div>
    </header>
  );
}
