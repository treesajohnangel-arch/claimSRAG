import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { inactivePolicy, mockApi, policy, policyDocument, renderApp, demoUser } from "../../test/utils";
import { validatePolicy } from "./PolicyForm";

const DETAIL = `/policies/${policy.id}`;

function detailRoutes(documents = [policyDocument]) {
  return {
    [`GET ${DETAIL}`]: { status: 200, body: policy },
    [`GET ${DETAIL}/documents`]: { status: 200, body: documents },
  };
}

describe("policy list", () => {
  it("shows each policy's number, name, type and status as text", async () => {
    mockApi({
      "GET /policies": { status: 200, body: [policy, inactivePolicy] },
      [`GET /policies/${policy.id}/documents`]: { status: 200, body: [policyDocument] },
      [`GET /policies/${inactivePolicy.id}/documents`]: { status: 200, body: [] },
    });
    demoUser("STAFF");
    await renderApp("/policies");

    const table = await screen.findByRole("table");
    await waitFor(() => expect(within(table).getAllByRole("row")[1]).toHaveTextContent("Health1"));
    const rows = within(table).getAllByRole("row");
    expect(rows.slice(1).map((row) => within(row).getAllByRole("cell").slice(0, 5).map((c) => c.textContent))).toEqual([
      ["DH-100", "Family Health Plus", "Health", "1", "Active"],
      ["MT-7", "Motor Secure", "Motor", "0", "Inactive"],
    ]);
    expect(screen.getByRole("link", { name: "Open policy DH-100" })).toHaveAttribute("href", DETAIL);
  });

  it("shows a skeleton while loading, then an error with a retry", async () => {
    let calls = 0;
    mockApi({ "GET /policies": () => (++calls === 1 ? { status: 500, body: { detail: "boom" } } : { status: 200, body: [] }) });
    demoUser("STAFF");
    await renderApp("/policies");

    expect(screen.getByRole("status")).toHaveAttribute("aria-busy", "true");
    expect(await screen.findByRole("alert")).toHaveTextContent("Something went wrong on the server");
    expect(screen.queryByText("boom")).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No policies yet")).toBeInTheDocument();
  });

  it.each([
    ["COMPANY_ADMIN", "Create the first policy for your company. Then upload its PDF to make it searchable.", true],
    ["STAFF", "A company admin adds your company's policies.", false],
  ] as const)("an empty list guides a %s user to the next step", async (role, text, offersCreate) => {
    mockApi({ "GET /policies": { status: 200, body: [] } });
    demoUser(role);
    await renderApp("/policies");

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryAllByRole("link", { name: "New policy" }).length > 0).toBe(offersCreate);
  });
});

describe("finding a policy", () => {
  it("filters by number or name and by insurance type, and offers to clear a filter that matches nothing", async () => {
    mockApi({ "GET /policies": { status: 200, body: [policy, inactivePolicy] } });
    demoUser("STAFF");
    await renderApp("/policies");
    const user = userEvent.setup();
    const names = () => within(screen.getByRole("table")).getAllByRole("row").slice(1)
      .map((row) => within(row).getAllByRole("cell")[0]!.textContent);

    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Motor" }));
    expect(names()).toEqual(["MT-7"]);
    expect(screen.getByRole("button", { name: "Motor" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "All" }));
    await user.type(screen.getByLabelText("Search policies"), "family");
    expect(names()).toEqual(["DH-100"]);
    await user.click(screen.getByRole("button", { name: "Travel" }));

    expect(screen.getByText("No policies match")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(names()).toEqual(["DH-100", "MT-7"]);
  });
});

describe("role-based controls (a courtesy: the backend decides)", () => {
  it("offers an admin the policy and document management actions", async () => {
    mockApi(detailRoutes());
    demoUser("COMPANY_ADMIN");
    await renderApp(DETAIL);

    expect(await screen.findByRole("button", { name: "Edit details" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete policy" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Withdraw family-health-wording.pdf" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Upload a policy PDF" })).toBeInTheDocument();
  });

  it("shows staff the policy and its documents without management actions", async () => {
    mockApi(detailRoutes());
    demoUser("STAFF");
    await renderApp(DETAIL);

    expect(await screen.findByRole("cell", { name: "family-health-wording.pdf" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit details" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Withdraw|Rename|Delete/ })).toBeNull();
    expect(screen.queryByRole("region", { name: "Upload a policy PDF" })).toBeNull();
    expect(screen.queryByLabelText("PDF file")).toBeNull();
  });

  it("tells staff they can't create policies, even at the form's address", async () => {
    const { requests } = mockApi({});
    demoUser("STAFF");
    await renderApp("/policies/new");

    expect(screen.getByText("Only a company admin can create policies.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Policy number")).toBeNull();
    expect(requests).toEqual([]);
  });

  it("shows the backend's refusal when an action is not allowed", async () => {
    mockApi({ ...detailRoutes(), [`DELETE /documents/${policyDocument.id}`]: { status: 403, body: { detail: "Forbidden" } } });
    demoUser("COMPANY_ADMIN");
    await renderApp(DETAIL);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Delete family-health-wording.pdf" }));
    await user.click(screen.getByRole("button", { name: "Delete version" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Your role doesn't allow this action.");
    expect(screen.getByRole("cell", { name: "family-health-wording.pdf" })).toBeInTheDocument();
  });
});

describe("creating a policy", () => {
  it("validates for usability before sending", () => {
    expect(validatePolicy({ policy_number: " ", name: "", insurance_type: "" })).toEqual({
      policy_number: "Enter the policy number.",
      name: "Enter the policy name.",
      insurance_type: "Choose the insurance type.",
    });
    expect(validatePolicy({ policy_number: "x".repeat(101), name: "y".repeat(301), insurance_type: "HOME" })).toEqual({
      policy_number: "Use at most 100 characters.",
      name: "Use at most 300 characters.",
    });
  });

  it("shows field errors next to the fields and sends nothing", async () => {
    const { requests } = mockApi({});
    demoUser("COMPANY_ADMIN");
    await renderApp("/policies/new");

    await userEvent.setup().click(screen.getByRole("button", { name: "Create policy" }));

    expect(screen.getByLabelText("Policy number")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Choose the insurance type.")).toBeInTheDocument();
    expect(requests).toEqual([]);
  });

  it("creates the policy with trimmed values only, then opens it", async () => {
    const { requests } = mockApi({ "POST /policies": { status: 201, body: policy }, ...detailRoutes([]) });
    demoUser("COMPANY_ADMIN");
    await renderApp("/policies/new");
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Policy number"), "  DH-100 ");
    await user.type(screen.getByLabelText("Policy name"), "Family Health Plus");
    await user.selectOptions(screen.getByLabelText("Insurance type"), "HEALTH");
    await user.click(screen.getByRole("button", { name: "Create policy" }));

    expect(await screen.findByText("Policy created.")).toBeInTheDocument();
    expect(requests.find((r) => r.method === "POST")?.body).toEqual({
      policy_number: "DH-100", name: "Family Health Plus", insurance_type: "HEALTH",
    });
    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
  });

  it("puts a duplicate number back on the number field", async () => {
    mockApi({ "POST /policies": { status: 409, body: { detail: "A policy with this number already exists" } } });
    demoUser("COMPANY_ADMIN");
    await renderApp("/policies/new");
    const user = userEvent.setup();

    await user.type(screen.getByLabelText("Policy number"), "DH-100");
    await user.type(screen.getByLabelText("Policy name"), "Again");
    await user.selectOptions(screen.getByLabelText("Insurance type"), "HEALTH");
    await user.click(screen.getByRole("button", { name: "Create policy" }));

    expect(await screen.findByText("A policy with this number already exists in your workspace.")).toBeInTheDocument();
    expect(screen.getByLabelText("Policy number")).toHaveAttribute("aria-invalid", "true");
  });
});

describe("policy details", () => {
  it("says a policy is not found, without offering a pointless retry", async () => {
    mockApi({ [`GET ${DETAIL}`]: { status: 404, body: { detail: "Policy not found" } } });
    demoUser("STAFF");
    await renderApp(DETAIL);

    expect(await screen.findByText("Policy not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("explains why an indexed document can't be deleted", async () => {
    mockApi({
      ...detailRoutes(),
      [`DELETE /documents/${policyDocument.id}`]: { status: 409, body: { detail: "The document still has embeddings" } },
    });
    demoUser("COMPANY_ADMIN");
    await renderApp(DETAIL);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Delete family-health-wording.pdf" }));
    await user.click(screen.getByRole("button", { name: "Delete version" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Withdraw it instead");
  });

  it("withdraws a document version and shows its new status", async () => {
    const { requests } = mockApi({
      ...detailRoutes(),
      [`PUT /documents/${policyDocument.id}/status`]: { status: 200, body: { ...policyDocument, status: "INACTIVE" } },
    });
    demoUser("COMPANY_ADMIN");
    await renderApp(DETAIL);

    await userEvent.setup().click(await screen.findByRole("button", { name: "Withdraw family-health-wording.pdf" }));

    expect(await screen.findByRole("button", { name: "Use again family-health-wording.pdf" })).toBeInTheDocument();
    expect(screen.getByText("Inactive")).toBeInTheDocument();
    expect(requests.at(-1)?.body).toEqual({ status: "INACTIVE" });
  });
});
