import { defineConfig } from "@playwright/test";

// The web package has no @types/node; declare the one Node global this file reads.
declare const process: { env: Record<string, string | undefined> };

export default defineConfig({
  testDir: "./tests/browser",
  // Each worker starts its own fixture server (tests/browser/fixtures.ts).
  workers: 4,
  fullyParallel: true,
  timeout: 30000,
  expect: { timeout: 5000 },
  reporter: "list",
  outputDir: "test-results",
  use: {
    channel: process.env.MANGACOUCH_BROWSER_CHANNEL || undefined,
    headless: true,
    // A first visit follows the browser's language; the specs read the Chinese interface.
    locale: "zh-CN",
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
});
