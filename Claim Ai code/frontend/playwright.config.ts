import { defineConfig } from "@playwright/test";

// End-to-end checks against a real local ClaimsRAG: the backend (with PostgreSQL and Ollama), the Vite dev server,
// and the demo workspace it opens into (see e2e/claimsrag.spec.ts). They use the installed Google Chrome, so no
// browser download is needed. Run with `npm run e2e` once the servers are up.
export default defineConfig({
  testDir: "./e2e",
  timeout: 240_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://127.0.0.1:5173",
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 900 },
    trace: "off",
  },
});
