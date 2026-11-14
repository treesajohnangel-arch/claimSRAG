/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// The browser talks only to its own origin under /api. In development, Vite forwards /api to the ClaimsRAG backend
// (CLAIMSRAG_API_URL, default http://127.0.0.1:8000), so the backend needs no CORS configuration. CLAIMSRAG_API_URL
// has no VITE_ prefix, so it is never bundled into the browser code.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const backend = env.CLAIMSRAG_API_URL || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: { "/api": { target: backend, changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, "") } },
    },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: { modules: { classNameStrategy: "non-scoped" } },
    },
  };
});
