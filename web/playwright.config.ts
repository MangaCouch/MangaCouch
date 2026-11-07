import { defineConfig } from "@playwright/test";

// The web package has no @types/node; declare the one Node global this file reads.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  timeout: 30000,
  expect: { timeout: 5000 },
  reporter: "list",
  outputDir: "test-results",
  use: {
    baseURL: "http://127.0.0.1:8793",
    channel: process.env.MANGACOUCH_BROWSER_CHANNEL || undefined,
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 1000 } } },
    {
      name: "mobile",
      use: {
        viewport: { width: 390, height: 844 },
        isMobile: true,
        hasTouch: true,
      },
    },
  ],
  webServer: {
    command: "uv run python tests/ui_browser_server.py --port 8793",
    cwd: "..",
    url: "http://127.0.0.1:8793",
    timeout: 60000,
    reuseExistingServer: false,
  },
});
