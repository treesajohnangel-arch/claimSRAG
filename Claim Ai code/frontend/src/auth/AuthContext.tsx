import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, MESSAGES, configureClient } from "../api/client";
import { api } from "../api/endpoints";
import type { DemoSessionResponse, SignedInUser } from "../api/types";

// ClaimsRAG opens straight into one company's workspace, with no sign-in screen (D-036). The backend's demo session
// issues a normal access token for the one user and company configured on the server. This provider fetches that
// token when the app starts, keeps it in memory only (never in storage: a reload simply fetches a new one), renews it
// shortly before it expires, and renews it once more if the server rejects it. The backend stays the authority: it
// verifies the token on every request and scopes every request to the token's company.

export interface Session {
  token: string;
  expiresAt: number; // epoch milliseconds
  user: SignedInUser;
  company: { name: string; slug: string };
}

export type SessionStatus = "opening" | "ready" | "unavailable";

interface SessionState {
  status: SessionStatus;
  session: Session | null;
  /** Why the workspace could not be opened (status "unavailable"). */
  error: ApiError | null;
}

interface SessionValue extends SessionState {
  /** Try to open the workspace again after it was unavailable. */
  retry: () => void;
}

const SessionContext = createContext<SessionValue | null>(null);

/** While the token is still valid but renewal failed, try again this often. */
const RETRY_MS = 10_000;

export function sessionFrom(response: DemoSessionResponse, now = Date.now()): Session {
  return {
    token: response.access_token,
    expiresAt: now + response.expires_in * 1000,
    user: response.user,
    company: response.company,
  };
}

/** When to renew: a minute before expiry, or after three quarters of a shorter lifetime. */
function renewalDelay(session: Session, now = Date.now()): number {
  const remaining = session.expiresAt - now;
  return remaining - Math.min(60_000, remaining / 4);
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<SessionState>({ status: "opening", session: null, error: null });
  const [failedRenewals, setFailedRenewals] = useState(0);
  const sessionRef = useRef<Session | null>(null);
  const inFlight = useRef<Promise<boolean> | null>(null);

  /** Fetch a fresh token (one request at a time). If that fails, the workspace stays open only while the current
   * token is still valid; otherwise it becomes unavailable and says why. */
  const renew = useCallback((): Promise<boolean> => {
    inFlight.current ??= api.demoSession().then(
      (response) => {
        const session = sessionFrom(response);
        sessionRef.current = session;
        setState({ status: "ready", session, error: null });
        return true;
      },
      (error: unknown) => {
        const current = sessionRef.current;
        if (!current || current.expiresAt <= Date.now()) {
          sessionRef.current = null;
          const failure = error instanceof ApiError ? error : new ApiError(0, "server", MESSAGES.server);
          setState({ status: "unavailable", session: null, error: failure });
        }
        return false;
      },
    ).finally(() => {
      inFlight.current = null;
    });
    return inFlight.current;
  }, []);

  // Requests read the token from here. A rejected token is renewed once; the request is then retried with the new one.
  useEffect(() => {
    configureClient(
      () => sessionRef.current?.token ?? null,
      (rejected) => {
        if (sessionRef.current && sessionRef.current.token !== rejected) return Promise.resolve(true); // already renewed
        sessionRef.current = null;
        return renew();
      },
    );
    void renew();
  }, [renew]);

  // Renew before the token expires, even without a request.
  useEffect(() => {
    if (!state.session) return;
    const delay = renewalDelay(state.session);
    const timer = window.setTimeout(() => {
      void renew().then((renewed) => {
        if (!renewed) setFailedRenewals((count) => count + 1);
      });
    }, delay > 0 && failedRenewals === 0 ? delay : RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [state.session, failedRenewals, renew]);

  const retry = useCallback(() => {
    setFailedRenewals(0);
    setState({ status: "opening", session: null, error: null });
    void renew();
  }, [renew]);

  const value = useMemo(() => ({ ...state, retry }), [state, retry]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSessionState(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSessionState must be used inside SessionProvider");
  return value;
}

/** The open workspace session. Only for components rendered behind SessionGate. */
export function useSession(): Session {
  const { session } = useSessionState();
  if (!session) throw new Error("useSession needs an open workspace");
  return session;
}
