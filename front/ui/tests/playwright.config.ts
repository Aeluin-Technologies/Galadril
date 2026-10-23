import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { resolve } from "node:path";

const origin = process.env.GALADRIL_UI_TEST_ORIGIN;
const port = Number(process.env.GALADRIL_UI_TEST_PORT ?? "3117");

export default defineConfig({
  testDir: ".",
  testMatch: "ui.spec.ts",
  workers: 1,
  maxFailures: 3,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 15_000 },
  reporter: "list",
  outputDir: resolve(
    process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? tmpdir(),
    "galadril-ui-results",
  ),
  use: {
    baseURL: origin ?? `http://127.0.0.1:${port}`,
    viewport: { width: 1440, height: 1040 },
  },
  webServer: origin
    ? undefined
    : {
        command:
          "node node_modules/nuxt/bin/nuxt.mjs build playground && node playground/.output/server/index.mjs",
        cwd: resolve(import.meta.dirname, ".."),
        url: `http://127.0.0.1:${port}`,
        timeout: 240_000,
        reuseExistingServer: false,
        env: {
          NODE_ENV: "production",
          NUXT_TELEMETRY_DISABLED: "1",
          NITRO_HOST: "127.0.0.1",
          NITRO_PORT: String(port),
        },
        stdout: "pipe",
        stderr: "pipe",
      },
});
