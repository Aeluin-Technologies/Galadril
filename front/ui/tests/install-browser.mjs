import { createRequire } from "node:module";
import { spawnSync } from "node:child_process";

const require = createRequire(import.meta.url);
if (!process.env.PLAYWRIGHT_BROWSERS_PATH)
  throw new Error("Set PLAYWRIGHT_BROWSERS_PATH to a writable browser cache");
const result = spawnSync(
  process.execPath,
  [
    require.resolve("@playwright/test/cli"),
    "install",
    "chromium",
    ...process.argv.slice(2),
  ],
  { stdio: "inherit", env: process.env },
);
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
