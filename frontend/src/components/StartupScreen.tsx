import type { ApiError } from "../api/client";
import { Brand } from "./AppLayout";
import { ErrorState } from "./States";
import styles from "./StartupScreen.module.css";

/** Why the workspace could not be opened, in words an operator can act on (never the server's own text). */
export function startupMessage(error: ApiError): string {
  switch (error.kind) {
    case "not_found":
      return "Demo access isn't configured on this server. Set DEMO_COMPANY_SLUG and DEMO_USER_EMAIL in "
        + "backend/.env to an existing company and user, then restart the backend.";
    case "unavailable":
      return "The workspace is unavailable right now. Check that the database is running and that the configured "
        + "demo company and user exist and are active, then try again.";
    default:
      return "ClaimsRAG can't reach the backend. Check that it is running, then try again.";
  }
}

/** Shown while the workspace session opens (briefly, so its text fades in late), or when it can't be opened. */
export function StartupScreen({ error, onRetry }: { error: ApiError | null; onRetry: () => void }) {
  return (
    <div className={styles.page}>
      <main className={styles.column}>
        <Brand />
        {error ? (
          <>
            <h1 className={styles.title}>The workspace couldn't be opened</h1>
            <ErrorState title="Not available" message={startupMessage(error)} onRetry={onRetry} />
          </>
        ) : (
          <p className={styles.opening} role="status" aria-busy="true" aria-label="Opening your workspace">
            Opening your workspace…
          </p>
        )}
      </main>
    </div>
  );
}
