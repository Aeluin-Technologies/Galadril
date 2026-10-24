import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { resolve, join, dirname } from "node:path";
import { stageFrontend } from "../../testing/stage.mjs";
import { createServer } from "node:net";

const stage = stageFrontend(resolve(process.cwd(), ".."), "dashboard");
process.on("exit", stage.cleanup);
const require = createRequire(resolve(stage.path, "package.json"));
const environment = { ...process.env };
if (!environment.GALADRIL_UI_TEST_ORIGIN) {
  const reservation = createServer();
  await new Promise((resolve, reject) => {
    reservation.once("error", reject);
    reservation.listen(0, "127.0.0.1", resolve);
  });
  const address = reservation.address();
  if (!address || typeof address === "string")
    throw new Error("Missing fixture port");
  environment.GALADRIL_UI_TEST_PORT = String(address.port);
  await new Promise((resolve, reject) =>
    reservation.close((error) => (error ? reject(error) : resolve())),
  );
}
const build =
  environment.GALADRIL_UI_TEST_DEV === "1"
    ? { status: 0 }
    : spawnSync(
        process.execPath,
        [
          resolve(
            dirname(require.resolve("nuxt/package.json")),
            "bin/nuxt.mjs",
          ),
          "build",
          "--envName",
          "ui-test",
        ],
        {
          cwd: stage.path,
          env: { ...environment, NUXT_TELEMETRY_DISABLED: "1" },
          stdio: "inherit",
          timeout: 240000,
        },
      );
if (build.status !== 0)
  throw new Error("Pipeline browser fixture build failed");
const runner = spawn(
  process.execPath,
  [
    require.resolve("@playwright/test/cli"),
    "test",
    "--config",
    join(stage.path, "tests/playwright-pipeline.config.ts"),
    ...process.argv.slice(2),
  ],
  { stdio: "inherit", env: environment, cwd: stage.path },
);
runner.on("error", () => {
  process.exitCode = 1;
});
runner.on("exit", (code) => {
  process.exitCode = code ?? 1;
});
process.on("SIGTERM", () => runner.kill("SIGTERM"));
process.on("SIGINT", () => runner.kill("SIGINT"));
