import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The FastAPI backend (`issue-agent dashboard --serve 8321`) binds loopback and
// serves both /api and the built SPA. In dev we run Vite on :5173 and proxy the
// API through, so the same relative URLs work in both modes.
const API_TARGET = process.env.DASHBOARD_API ?? "http://127.0.0.1:8321";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
