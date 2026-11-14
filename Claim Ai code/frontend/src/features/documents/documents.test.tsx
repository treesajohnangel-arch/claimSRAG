import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { inactivePolicy, mockApi, policy, policyDocument, renderApp, demoUser } from "../../test/utils";

const WITHDRAWN = { ...policyDocument, id: "55555555-5555-4555-8555-555555555555", policy_id: inactivePolicy.id,
  original_filename: "motor-wording.pdf", version: 1, status: "INACTIVE" as const, created_at: "2026-10-01T10:00:00Z" };

describe("the documents list", () => {
  it("lists every policy's document versions from the API, newest first, with links", async () => {
    mockApi({
      "GET /policies": { status: 200, body: [policy, inactivePolicy] },
      [`GET /policies/${policy.id}/documents`]: { status: 200, body: [policyDocument] },
      [`GET /policies/${inactivePolicy.id}/documents`]: { status: 200, body: [WITHDRAWN] },
    });
    demoUser("STAFF");
    await renderApp("/documents");

    const rows = within(await screen.findByRole("table")).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell").slice(0, 4).map((c) => c.textContent))).toEqual([
      ["family-health-wording.pdf", "DH-100 Family Health Plus", "2", "In use"],
      ["motor-wording.pdf", "MT-7 Motor Secure", "1", "Inactive"],
    ]);
    expect(screen.getByRole("link", { name: "family-health-wording.pdf" })).toHaveAttribute("href", `/documents/${policyDocument.id}`);
  });

  it("says when there are no documents, and when some couldn't be loaded", async () => {
    mockApi({
      "GET /policies": { status: 200, body: [policy, inactivePolicy] },
      [`GET /policies/${policy.id}/documents`]: { status: 200, body: [] },
      [`GET /policies/${inactivePolicy.id}/documents`]: { status: 500, body: { detail: "boom" } },
    });
    demoUser("COMPANY_ADMIN");
    await renderApp("/documents");

    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
    expect(screen.getByText("The documents of 1 policy couldn't be loaded.")).toBeInTheDocument();
    expect(screen.queryByText("boom")).toBeNull();
  });
});

describe("a document's page", () => {
  it("shows the source of truth: policy, version, status, size, fingerprint, and a search within it", async () => {
    mockApi({
      [`GET /documents/${policyDocument.id}`]: { status: 200, body: policyDocument },
      [`GET /policies/${policy.id}`]: { status: 200, body: policy },
    });
    demoUser("STAFF");
    await renderApp(`/documents/${policyDocument.id}`);

    expect(await screen.findByRole("heading", { name: "family-health-wording.pdf", level: 1 })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "DH-100 Family Health Plus" })).toHaveAttribute("href", `/policies/${policy.id}`);
    expect(screen.getByText("a".repeat(64))).toBeInTheDocument(); // the stored SHA-256, as recorded
    expect(screen.getByText("In use")).toBeInTheDocument();
    for (const link of screen.getAllByRole("link", { name: "Search this document" })) {
      expect(link).toHaveAttribute("href", `/search?policy_id=${policy.id}&document_id=${policyDocument.id}`);
    }
    expect(screen.getByText(/A page-by-page outline isn't available here yet/)).toBeInTheDocument();
  });

  it("is not found for a document the workspace doesn't have, without a pointless retry", async () => {
    mockApi({ [`GET /documents/${policyDocument.id}`]: { status: 404, body: { detail: "Document not found" } } });
    demoUser("STAFF");
    await renderApp(`/documents/${policyDocument.id}`);

    expect(await screen.findByText("Document not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });
});
