import { defineConfig } from "vitest/config";

// The web app is served by the FastAPI backend, so we build into the Python package
// (src/solopm/web/dist) and the dev server proxies /api to the running backend.
export default defineConfig({
  build: {
    outDir: "../src/solopm/web/dist",
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
