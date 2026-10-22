import { defineConfig } from "@playwright/test";
import { resolve } from "node:path";
import { tmpdir } from "node:os";
const port = Number(process.env.GALADRIL_UI_TEST_PORT);
const origin = process.env.GALADRIL_UI_TEST_ORIGIN;
export default defineConfig({
  testDir: ".",
  testMatch: "dashboard-ui.spec.ts",
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  outputDir: resolve(
    process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? tmpdir(),
    "dashboard-ui-results",
  ),
  use: {
    baseURL: origin ?? `http://127.0.0.1:${port}`,
    ignoreHTTPSErrors: origin === "https://localhost:8080",
    viewport: { width: 1440, height: 1000 },
  },
  webServer: origin
    ? undefined
    : {
        command:
          "node node_modules/nuxt/bin/nuxt.mjs build --envName ui-test && node .output/server/index.mjs",
        cwd: resolve(import.meta.dirname, ".."),
        url: `http://127.0.0.1:${port}/login`,
        timeout: 240_000,
        stdout: "pipe",
        stderr: "pipe",
        env: {
          NODE_ENV: "production",
          NUXT_TELEMETRY_DISABLED: "1",
          NITRO_HOST: "127.0.0.1",
          NITRO_PORT: String(port),
        },
      },
});
