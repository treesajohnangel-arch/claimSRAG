import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { inactivePolicy, mockApi, policy, policyDocument, renderApp, demoUser } from "../../test/utils";

const WITHDRAWN = { ...policyDocument, id: "66666666-6666-4666-8666-666666666666", status: "INACTIVE" as const };

describe("the overview", () => {
  it("shows only figures computed from the workspace's own policies and documents", async () => {
    mockApi({
      "GET /policies": { status: 200, body: [policy, inactivePolicy] },
      [`GET /policies/${policy.id}/documents`]: { status: 200, body: [policyDocument, WITHDRAWN] },
      [`GET /policies/${inactivePolicy.id}/documents`]: { status: 200, body: [] },
    });
    demoUser("COMPANY_ADMIN");
    await renderApp("/");

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Good (morning|afternoon|evening), Asha$/);
    const stats = await screen.findByRole("region", { name: "Workspace at a glance" });
    await waitFor(() => expect(stats).toHaveTextContent("1Documents in use"));
    expect(stats).toHaveTextContent("1Active policies1 inactive"); // DH-100 active, MT-7 inactive
    expect(stats).toHaveTextContent("1 of 4Insurance types coveredHealth"); // only active policies count
    expect(stats.textContent).not.toMatch(/question|claim|accuracy|%/i); // nothing the backend doesn't record
    const recent = screen.getByRole("region", { name: "Recently updated policies" });
    expect(within(recent).getByRole("link", { name: /DH-100 Family Health Plus/ })).toHaveTextContent("2 documents");
  });

  it("offers each role the tasks it can do", async () => {
    mockApi({ "GET /policies": { status: 200, body: [] } });
    demoUser("STAFF");
    await renderApp("/");

    const tasks = screen.getByRole("navigation", { name: "Start a task" });
    expect(within(tasks).getAllByRole("link").map((link) => link.textContent)).toEqual([
      expect.stringContaining("Browse policies"), expect.stringContaining("Search evidence"),
      expect.stringContaining("Ask a question")]);
  });
});
