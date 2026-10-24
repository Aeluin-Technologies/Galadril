import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";
export default defineConfig({
  testDir: ".",
  testMatch: "pipeline-ui.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 90000,
  outputDir: resolve(
    process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? "/tmp",
    "pipeline-browser-results",
  ),
  use: {
    actionTimeout: 15000,
    trace: "retain-on-failure",
    baseURL: `http://127.0.0.1:${process.env.GALADRIL_UI_TEST_PORT}`,
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command: "node .output/server/index.mjs",
    cwd: resolve(import.meta.dirname, ".."),
    url: `http://127.0.0.1:${process.env.GALADRIL_UI_TEST_PORT}/login`,
    timeout: 120000,
    stdout: "pipe",
    stderr: "pipe",
    env: {
      PORT: process.env.GALADRIL_UI_TEST_PORT ?? "3102",
      HOST: "127.0.0.1",
      NUXT_TELEMETRY_DISABLED: "1",
    },
  },
});
