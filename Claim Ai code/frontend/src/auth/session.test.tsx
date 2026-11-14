import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TOKEN, demoUser, mockApi, policy, renderApp, renderAppNow } from "../test/utils";

const DEMO = demoUser();
const GREETING = /^Good (morning|afternoon|evening), Asha$/;

describe("opening ClaimsRAG", () => {
  it("opens directly on the dashboard, with no sign-in screen", async () => {
    const { requests, sessionRequests } = mockApi({ "GET /policies": { status: 200, body: [] } });

    await renderApp("/");

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(GREETING);
    expect(screen.queryByLabelText(/password/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /sign in/i })).toBeNull();
    // The session request carries nothing: the server decides the company and user.
    expect(sessionRequests).toHaveLength(1);
    expect(sessionRequests[0]).toMatchObject({ method: "POST", body: undefined });
    expect(sessionRequests[0]?.headers.Authorization).toBeUndefined();
    // The workspace's own requests carry the token, and never a company, tenant or role.
    await waitFor(() => expect(requests.some((r) => r.path === "/policies")).toBe(true));
    for (const request of requests) {
      expect(request.headers.Authorization).toBe(`Bearer ${TOKEN}`);
      expect(`${request.path} ${JSON.stringify(request.body ?? {})}`).not.toMatch(/company|tenant|role/);
    }
  });

  it.each(["/login", "/register"])("treats an old %s link as the dashboard", async (path) => {
    mockApi({ "GET /policies": { status: 200, body: [] } });

    await renderApp(path);

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(GREETING);
    expect(screen.queryByLabelText(/password/i)).toBeNull();
  });

  it("opens any page directly", async () => {
    mockApi({ "GET /policies": { status: 200, body: [policy] } });

    await renderApp("/policies");

    expect(screen.getByRole("heading", { level: 1, name: "Policies" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: "Open policy DH-100" })).toBeInTheDocument();
  });

  it("shows which company and user the workspace opened as, with no sign-out", async () => {
    mockApi({ "GET /policies": { status: 200, body: [] } });

    await renderApp("/");

    const rail = document.getElementById("app-navigation")!;
    expect(within(rail).getByText(DEMO.company.name)).toBeInTheDocument();
    expect(within(rail).getByText("demo-health")).toBeInTheDocument();
    expect(within(rail).getByText("Asha Menon")).toBeInTheDocument();
    expect(within(rail).getByText("Company admin")).toBeInTheDocument();
    expect(within(rail).getByText("Demo workspace: opened without sign-in.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /sign out/i })).toBeNull();
  });

  it("keeps the token in memory only: never in storage or on the page", async () => {
    mockApi({ "GET /policies": { status: 200, body: [] } });

    await renderApp("/");

    expect(sessionStorage.length).toBe(0);
    expect(Object.values(localStorage).join()).not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });

  it("says it is opening while the session request is under way", async () => {
    let release: () => void = () => {};
    mockApi({
      "POST /auth/demo-session": () => new Promise((resolve) => { release = () => resolve({ status: 200, body: DEMO }); }),
      "GET /policies": { status: 200, body: [] },
    });

    renderAppNow("/");

    expect(screen.getByRole("status", { name: "Opening your workspace" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    release();
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(GREETING);
  });
});

describe("when the workspace can't be opened", () => {
  it("explains that demo access is off, and opens once it is on", async () => {
    let calls = 0;
    mockApi({
      "POST /auth/demo-session": () => (++calls === 1 ? { status: 404, body: { detail: "Not Found" } } : { status: 200, body: DEMO }),
      "GET /policies": { status: 200, body: [] },
    });

    await renderApp("/");

    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("The workspace couldn't be opened");
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Set DEMO_COMPANY_SLUG and DEMO_USER_EMAIL in backend/.env");
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull();
    await userEvent.setup().click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { level: 1, name: GREETING })).toBeInTheDocument();
  });

  it.each([
    [503, "The workspace is unavailable right now."],
    [500, "ClaimsRAG can't reach the backend."],
  ])("explains a %i without the server's text", async (status, message) => {
    mockApi({ "POST /auth/demo-session": { status, body: { detail: "SERVER TEXT" } } });

    await renderApp("/");

    expect(screen.getByRole("alert")).toHaveTextContent(message);
    expect(screen.queryByText(/SERVER TEXT/)).toBeNull();
  });
});

describe("keeping the workspace open", () => {
  it("renews a token the server rejects, and retries the request without leaving the page", async () => {
    let sessions = 0;
    const { requests } = mockApi({
      "POST /auth/demo-session": () => ({ status: 200, body: { ...DEMO, access_token: ++sessions === 1 ? TOKEN : "renewed" } }),
      "GET /policies": (r) => (r.headers.Authorization === "Bearer renewed"
        ? { status: 200, body: [policy] } : { status: 401, body: { detail: "Invalid or expired token" } }),
    });

    await renderApp("/policies");

    expect(await screen.findByRole("link", { name: "Open policy DH-100" })).toBeInTheDocument();
    expect(requests.filter((r) => r.path === "/policies").map((r) => r.headers.Authorization))
      .toEqual([`Bearer ${TOKEN}`, "Bearer renewed"]); // rejected, then retried once with the renewed token
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renews the token before it expires", async () => {
    let sessions = 0;
    const { requests, sessionRequests } = mockApi({
      "POST /auth/demo-session": () => (++sessions === 1
        ? { status: 200, body: { ...DEMO, expires_in: 1 } }
        : { status: 200, body: { ...DEMO, access_token: "renewed" } }),
      "GET /policies": { status: 200, body: [] },
    });
    await renderApp("/");

    await waitFor(() => expect(sessionRequests).toHaveLength(2), { timeout: 3000 });
    await userEvent.setup().click(screen.getAllByRole("link", { name: "Policies" })[0]!);

    await waitFor(() => expect(requests.at(-1)?.headers.Authorization).toBe("Bearer renewed"));
    expect(screen.getByRole("heading", { level: 1, name: "Policies" })).toBeInTheDocument();
  });
});
