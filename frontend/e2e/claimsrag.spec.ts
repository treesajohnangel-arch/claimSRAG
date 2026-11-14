import { expect, test } from "@playwright/test";

// The whole ClaimsRAG flow in a real browser, against the real backend, PostgreSQL, embedding model and local LLM.
// ClaimsRAG opens directly into the demo company's workspace, with no sign-in (D-036):
//
//   open → dashboard → create a HEALTH policy → upload its PDF → indexed → search → ask → grounded answer with a page
//   citation → abstain on an unsupported question
//
// Prerequisites (docs/IMPLEMENTATION_LOG.md, D-036): the backend running with DEMO_COMPANY_SLUG and DEMO_USER_EMAIL
// naming an active COMPANY_ADMIN of an active company, `npm run dev`, Ollama, and E2E_SAMPLE_PDF: a PDF with a text
// layer whose page 3 states the maternity waiting period (`python -m scripts.workspace sample-pdf --out <path>`).
// Each run adds one policy to the demo workspace and withdraws its document at the end, so later runs and other
// policies are not answered from it.

const samplePdf = process.env.E2E_SAMPLE_PDF;
const GREETING = /^Good (morning|afternoon|evening), /;

test("ClaimsRAG opens directly on the dashboard, with no sign-in", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1, name: GREETING })).toBeVisible();
  await expect(page.getByText("Demo workspace: opened without sign-in.")).toBeVisible();
  await expect(page.locator('input[type="password"]')).toHaveCount(0);

  // An old sign-in or registration link opens the workspace too.
  for (const path of ["/login", "/register"]) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("heading", { level: 1, name: GREETING })).toBeVisible();
  }

  // Any page opens directly.
  await page.goto("/ask");
  await expect(page.getByRole("heading", { level: 1, name: "Ask ClaimsRAG" })).toBeVisible();
});

test("a policy PDF is uploaded, indexed, searched, and answered from with a citation", async ({ page }) => {
  test.skip(!samplePdf, "Set E2E_SAMPLE_PDF to the sample policy PDF");
  const number = `E2E-${Date.now().toString(36).toUpperCase()}`;
  const policyLabel = `${number} Family Health Plan`;
  const question = "What is the waiting period for maternity expenses?";
  const unsupported = "Does the policy offer a no-claim bonus?";

  // Create a HEALTH policy.
  await page.goto("/");
  await page.getByRole("link", { name: /Create a policy/ }).click();
  await page.getByLabel("Policy number").fill(number);
  await page.getByLabel("Policy name").fill("Family Health Plan");
  await page.getByLabel("Insurance type").selectOption("HEALTH");
  await page.getByRole("button", { name: "Create policy" }).click();
  await expect(page.getByText("Policy created.")).toBeVisible();

  // Upload the PDF: the backend stores, extracts, chunks and indexes it.
  await page.getByLabel("PDF file").setInputFiles(samplePdf!);
  await page.getByRole("button", { name: "Upload and index" }).click();
  await expect(page.getByText(/Ready: .+ \(version 1\) is indexed, 4 pages in \d+ passages\./)).toBeVisible({ timeout: 60_000 });
  const documents = page.getByRole("table", { name: "Documents of this policy" });
  await expect(documents.getByRole("cell", { name: "In use" })).toBeVisible();
  const policyUrl = page.url();

  try {
    // Search this policy for a phrase from the PDF: passages with page and section provenance, not answers.
    await page.getByRole("link", { name: "Search", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Search evidence" })).toBeVisible();
    await page.getByLabel("Policy", { exact: true }).selectOption({ label: policyLabel });
    await page.getByLabel("Search for").fill("waiting period for maternity expenses");
    await page.getByRole("button", { name: "Search" }).click();
    const result = page.getByRole("article").first();
    await expect(result).toContainText("Maternity expenses");
    await expect(result.getByRole("list", { name: "Source" })).toContainText(policyLabel);
    await expect(result.getByRole("list", { name: "Source" })).toContainText("Page 3");

    // Ask a question the PDF answers: a grounded answer with a page citation of the clause itself.
    await page.getByRole("link", { name: "Ask", exact: true }).click();
    await expect(page.getByRole("heading", { level: 1, name: "Ask ClaimsRAG" })).toBeVisible();
    await page.getByLabel("Policy", { exact: true }).selectOption({ label: policyLabel });
    await page.getByLabel("Your question").fill(question);
    await page.getByRole("button", { name: "Ask" }).click();
    const answered = page.getByRole("region", { name: `Question: ${question}` });
    await expect(answered.getByRole("heading", { name: "Answered from policy evidence" })).toBeVisible({ timeout: 200_000 });
    await expect(answered).toContainText("9 months");
    const sources = answered.getByRole("complementary", { name: "Sources" }).getByRole("list", { name: "Source" });
    const clause = sources.filter({ hasText: "Page 3" }).filter({ hasText: "SECTION 3 - WAITING PERIODS" });
    await expect(clause).toHaveCount(1);
    await expect(clause).toContainText(policyLabel);
    await expect(clause).toContainText("version 1");

    // Ask what the PDF does not say: abstain, with no citation.
    await page.getByLabel("Your question").fill(unsupported);
    await page.getByRole("button", { name: "Ask" }).click();
    const refused = page.getByRole("region", { name: `Question: ${unsupported}` });
    await expect(refused.getByRole("heading", { name: "Insufficient evidence" })).toBeVisible({ timeout: 200_000 });
    await expect(refused.getByRole("complementary", { name: "Sources" })).toHaveCount(0);
  } finally {
    // Withdraw the uploaded version, so this run's copy of the sample is no longer used as evidence.
    await page.goto(policyUrl);
    await page.getByRole("button", { name: /^Withdraw / }).click();
    await expect(page.getByRole("table", { name: "Documents of this policy" }).getByRole("cell", { name: "Inactive" }))
      .toBeVisible();
  }
});

test("a policy is created and deleted", async ({ page }) => {
  const number = `E2E-${Date.now().toString(36).toUpperCase()}-D`;
  await page.goto("/policies/new");
  await page.getByLabel("Policy number").fill(number);
  await page.getByLabel("Policy name").fill("End-to-end check");
  await page.getByLabel("Insurance type").selectOption("TRAVEL");
  await page.getByRole("button", { name: "Create policy" }).click();
  await expect(page.getByText("Policy created.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Upload a policy PDF" })).toBeVisible();

  await page.getByRole("button", { name: "Delete policy" }).click();
  await page.getByRole("group", { name: "Confirm policy deletion" }).getByRole("button", { name: "Delete policy" }).click();
  await expect(page).toHaveURL(/\/policies$/);
  await expect(page.getByRole("cell", { name: number })).toHaveCount(0);
});

test("the backend still decides: no client tenant, and no request without a token", async ({ request }) => {
  // The demo session ignores anything the client sends: the server's settings choose the company and user.
  const plain = await request.post("/api/auth/demo-session");
  expect(plain.status()).toBe(200);
  const plainBody = await plain.json();
  const steered = await request.post("/api/auth/demo-session", {
    data: { company_slug: "another-company", company_id: "00000000-0000-4000-8000-000000000000", role: "STAFF" },
    headers: { "X-Company-Id": "00000000-0000-4000-8000-000000000000" },
  });
  const steeredBody = await steered.json();
  expect([steeredBody.user, steeredBody.company]).toEqual([plainBody.user, plainBody.company]);
  expect(JSON.stringify(plainBody)).not.toMatch(/company_id|status|password/);

  // Every workspace route still needs the token, and a tenant in the body is refused.
  expect((await request.get("/api/policies")).status()).toBe(401);
  const headers = { Authorization: `Bearer ${plainBody.access_token}` };
  expect((await request.get("/api/policies", { headers })).status()).toBe(200);
  const tenantInBody = await request.post("/api/search", {
    headers, data: { query: "maternity", company_id: "00000000-0000-4000-8000-000000000000" },
  });
  expect(tenantInBody.status()).toBe(422);
});
