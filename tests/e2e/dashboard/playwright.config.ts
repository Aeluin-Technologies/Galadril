import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: ["dashboard.spec.mjs", "pengolo.spec.ts"],
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["junit", { outputFile: "/tmp/results/junit.xml" }]],
  outputDir: "/tmp/results/browser",
  use: {
    baseURL: process.env.GALADRIL_TEST_ORIGIN,
    browserName: "chromium",
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
    // Traces contain only ephemeral fixture accounts, never a developer session.
    trace: "retain-on-failure",
  },
});
