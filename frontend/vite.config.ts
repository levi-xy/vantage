/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// In dev, the browser talks only to the backend (AGENTS.md hard rule 4).
const backend = process.env.VANTAGE_BACKEND_URL ?? "http://127.0.0.1:8000";

// The release version, from the image build ("dev" for local builds and CI).
const version = process.env.VANTAGE_VERSION?.trim() || "dev";

export default defineConfig({
  define: {
    __VANTAGE_VERSION__: JSON.stringify(version),
    // The changelog section anchor, computed by the release workflow (empty = none).
    __CHANGELOG_ANCHOR__: JSON.stringify(process.env.VANTAGE_CHANGELOG_ANCHOR?.trim() || ""),
    __VANTAGE_BUILD_DATE__: JSON.stringify(process.env.VANTAGE_BUILD_DATE?.trim() || ""),
  },
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": backend,
      "/healthz": backend,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
  },
});
