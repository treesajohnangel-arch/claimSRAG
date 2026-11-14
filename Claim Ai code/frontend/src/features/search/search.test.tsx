import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AMBULANCE, MATERNITY, hit, mockApi, policy, policyDocument, renderApp, searchResponse, demoUser } from "../../test/utils";

const DIRECTORY = {
  "GET /policies": { status: 200, body: [policy] },
  [`GET /documents/${policyDocument.id}`]: { status: 200, body: policyDocument },
};

async function search(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Search for"), text);
  await user.click(screen.getByRole("button", { name: "Search" }));
  return user;
}

describe("evidence search", () => {
  it("lists retrieved passages with their provenance, labelled as passages rather than answers", async () => {
    const { requests } = mockApi({
      ...DIRECTORY,
      "POST /search": { status: 200, body: searchResponse([hit(1, MATERNITY), hit(2, AMBULANCE, { keyword_rank: null })]) },
    });
    demoUser("STAFF");
    await renderApp("/search");

    await search("maternity waiting period");

    expect(await screen.findByRole("heading", { name: /2 passages for/ })).toBeInTheDocument();
    expect(screen.getByText(/they are not answers/)).toBeInTheDocument();
    const first = screen.getAllByRole("article")[0]!;
    expect(within(first).getByText("Result 1")).toBeInTheDocument();
    expect(within(first).getByText("Matched #1 by meaning and #1 by keywords")).toBeInTheDocument();
    expect(await within(first).findByText("DH-100 Family Health Plus")).toBeInTheDocument();
    expect(await within(first).findByText("family-health-wording.pdf, version 2")).toBeInTheDocument();
    expect(within(first).getByText("Page 3")).toBeInTheDocument();
    expect(within(first).getByRole("heading", { name: "SECTION 1" })).toBeInTheDocument();
    expect(within(first).getByRole("list", { name: "Source" })).toHaveTextContent("SECTION 1");
    // The passage is the exact policy text, with the search's words highlighted (never changed).
    const passage = within(first).getByRole("blockquote");
    expect(passage).toHaveTextContent(MATERNITY);
    expect([...passage.querySelectorAll("mark")].map((m) => m.textContent)).toEqual(
      expect.arrayContaining(["Maternity", "waiting", "period"]));
    expect(within(screen.getAllByRole("article")[1]!).getByText("Matched #2 by meaning")).toBeInTheDocument();
    expect(requests.find((r) => r.path === "/search")?.body).toEqual({ query: "maternity waiting period", top_k: 5, filters: {} });
  });

  it("sends only the filters the user chose, and never a company, tenant or role", async () => {
    const { requests } = mockApi({ ...DIRECTORY, "POST /search": { status: 200, body: searchResponse([]) } });
    demoUser("STAFF");
    await renderApp("/search");
    const user = userEvent.setup();

    await screen.findByRole("option", { name: "DH-100 Family Health Plus" });
    await user.selectOptions(screen.getByLabelText("Policy"), policy.id);
    await user.selectOptions(screen.getByLabelText("Results"), "10");
    await search("ambulance");

    await screen.findByText("No matching evidence");
    const body = requests.find((r) => r.path === "/search")?.body as Record<string, unknown>;
    expect(body).toEqual({ query: "ambulance", top_k: 10, filters: { policy_id: policy.id } });
    expect(JSON.stringify(body)).not.toMatch(/company|tenant|role|permission/);
  });

  it("narrows to one document of the chosen policy", async () => {
    const { requests } = mockApi({ ...DIRECTORY, [`GET /policies/${policy.id}/documents`]: { status: 200, body: [policyDocument] },
      "POST /search": { status: 200, body: searchResponse([]) } });
    demoUser("STAFF");
    await renderApp("/search");
    const user = userEvent.setup();

    expect(screen.getByLabelText("Document")).toBeDisabled();
    await screen.findByRole("option", { name: "DH-100 Family Health Plus" });
    await user.selectOptions(screen.getByLabelText("Policy"), policy.id);
    await screen.findByRole("option", { name: "family-health-wording.pdf, version 2" });
    await user.selectOptions(screen.getByLabelText("Document"), policyDocument.id);
    await search("maternity");

    await screen.findByText("No matching evidence");
    expect(requests.find((r) => r.path === "/search")?.body).toEqual({
      query: "maternity", top_k: 5, filters: { policy_id: policy.id, document_id: policyDocument.id } });
  });

  it("starts from the policy and document a document page links with", async () => {
    const { requests } = mockApi({ ...DIRECTORY, [`GET /policies/${policy.id}/documents`]: { status: 200, body: [policyDocument] },
      "POST /search": { status: 200, body: searchResponse([]) } });
    demoUser("STAFF");
    await renderApp(`/search?policy_id=${policy.id}&document_id=${policyDocument.id}`);

    await screen.findByRole("option", { name: "family-health-wording.pdf, version 2" });
    await search("ambulance");

    await screen.findByText("No matching evidence");
    expect((requests.find((r) => r.path === "/search")?.body as { filters: unknown }).filters).toEqual(
      { policy_id: policy.id, document_id: policyDocument.id });
  });

  it("previews each passage and opens the full evidence on request", async () => {
    mockApi({ ...DIRECTORY, "POST /search": { status: 200, body: searchResponse([hit(1, MATERNITY)]) } });
    demoUser("STAFF");
    await renderApp("/search");

    const user = await search("maternity");
    const toggle = await screen.findByRole("button", { name: "View evidence" });

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Hide evidence" })).toHaveAttribute("aria-expanded", "true");
  });

  it("explains an unavailable search service, with a retry", async () => {
    mockApi({ ...DIRECTORY, "POST /search": { status: 503, body: { detail: "Search is temporarily unavailable" } } });
    demoUser("STAFF");
    await renderApp("/search");

    await search("dental");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("temporarily unavailable");
    expect(within(alert).getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("asks for a search term instead of sending an empty one", async () => {
    const { requests } = mockApi(DIRECTORY);
    demoUser("STAFF");
    await renderApp("/search");

    await userEvent.setup().click(screen.getByRole("button", { name: "Search" }));

    expect(screen.getByText("Enter what you are looking for.")).toBeInTheDocument();
    expect(requests.some((r) => r.path === "/search")).toBe(false);
  });
});
