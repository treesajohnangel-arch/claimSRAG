import { useEffect, useState } from "react";
import { api } from "../api/endpoints";
import type { Policy, PolicyDocument } from "../api/types";

export interface Directory {
  policies: Map<string, Policy>;
  documents: Map<string, PolicyDocument>;
}

/** Names for the policies and documents that evidence points to, read from the tenant's own API so a passage can
 * say "Policy H-100, wording.pdf" instead of an id. Lookups that fail leave the label generic; evidence and
 * citations themselves are never changed. */
export function useDirectory(documentIds: readonly string[]): Directory {
  const [policies, setPolicies] = useState<Map<string, Policy>>(new Map());
  const [documents, setDocuments] = useState<Map<string, PolicyDocument>>(new Map());

  useEffect(() => {
    const controller = new AbortController();
    api.listPolicies(controller.signal).then(
      (list) => setPolicies(new Map(list.map((policy) => [policy.id, policy]))),
      () => undefined,
    );
    return () => controller.abort();
  }, []);

  const key = [...new Set(documentIds)].sort().join(",");
  useEffect(() => {
    const missing = key ? key.split(",").filter((id) => !documents.has(id)) : [];
    if (missing.length === 0) return;
    const controller = new AbortController();
    Promise.allSettled(missing.map((id) => api.getDocument(id, controller.signal))).then((results) => {
      if (controller.signal.aborted) return;
      setDocuments((current) => {
        const next = new Map(current);
        for (const result of results) if (result.status === "fulfilled") next.set(result.value.id, result.value);
        return next;
      });
    });
    return () => controller.abort();
  }, [key]); // `documents` is read, not watched: a fetched document never needs fetching again.

  return { policies, documents };
}
