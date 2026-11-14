import { Outlet } from "react-router";
import { StartupScreen } from "../components/StartupScreen";
import { useSessionState } from "./AuthContext";

/** Renders the workspace once its session is open; until then, or if it can't be opened, says so. This is not
 * security: every API request is authorised and tenant-scoped by the backend from the token. */
export function SessionGate() {
  const { status, error, retry } = useSessionState();
  if (status === "ready") return <Outlet />;
  return <StartupScreen error={status === "unavailable" ? error : null} onRetry={retry} />;
}
