import { useCallback, useEffect, useState, type DependencyList } from "react";
import { ApiError, MESSAGES, isAbort } from "../api/client";

export type AsyncState<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: ApiError };

export function toApiError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError(0, "server", MESSAGES.server);
}

/** Load data for a screen, cancelling the request if the screen goes away or the inputs change. */
export function useAsync<T>(load: (signal: AbortSignal) => Promise<T>, deps: DependencyList) {
  const [state, setState] = useState<AsyncState<T>>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    load(controller.signal).then(
      (data) => setState({ status: "ready", data }),
      (error: unknown) => {
        if (!isAbort(error)) setState({ status: "error", error: toApiError(error) });
      },
    );
    return () => controller.abort();
  }, [...deps, attempt]); // `load` is recreated each render; `deps` say when it really changes.

  const reload = useCallback(() => setAttempt((value) => value + 1), []);
  return { state, reload, setState };
}
