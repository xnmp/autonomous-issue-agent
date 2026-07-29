import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:4173",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: "npm run dev -- --host 127.0.0.1 --port 4173",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: false,
    },
    {
      command:
        "cd .. && UV_NO_CACHE=1 uv run --offline --no-sync python dashboard/e2e/server.py",
      url: "http://127.0.0.1:4174",
      reuseExistingServer: false,
    },
  ],
});
