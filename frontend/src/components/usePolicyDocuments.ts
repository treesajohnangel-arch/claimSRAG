import { useEffect, useState } from "react";
import { api } from "../api/endpoints";
import type { Policy, PolicyDocument } from "../api/types";

export interface PolicyDocuments {
  /** Each policy's documents; a policy whose list could not be loaded is absent. */
  byPolicy: Map<string, PolicyDocument[]>;
  loading: boolean;
}

/** The documents of each policy, from the tenant's own API (one request per policy). Used for counts and the
 * documents list; nothing here is estimated: a list that fails to load is simply not counted. */
export function usePolicyDocuments(policies: Policy[] | null): PolicyDocuments {
  const [state, setState] = useState<PolicyDocuments>({ byPolicy: new Map(), loading: true });
  const key = policies ? policies.map((p) => p.id).join(",") : null;

  useEffect(() => {
    if (!policies) return;
    const controller = new AbortController();
    setState({ byPolicy: new Map(), loading: true });
    Promise.allSettled(policies.map((policy) => api.listDocuments(policy.id, controller.signal))).then((results) => {
      if (controller.signal.aborted) return;
      const byPolicy = new Map<string, PolicyDocument[]>();
      results.forEach((result, index) => {
        if (result.status === "fulfilled") byPolicy.set(policies[index]!.id, result.value);
      });
      setState({ byPolicy, loading: false });
    });
    return () => controller.abort();
  }, [key]); // `policies` is read through `key`, its identity

  return state;
}
