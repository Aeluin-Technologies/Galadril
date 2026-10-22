import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import { resolve, join } from "node:path";
import { stageFrontend } from "../../testing/stage.mjs";
import { createServer } from "node:net";

const stage = stageFrontend(resolve(process.cwd(), ".."), "ui");
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
const runner = spawn(
  process.execPath,
  [
    require.resolve("@playwright/test/cli"),
    "test",
    "--config",
    join(stage.path, "tests/playwright.config.ts"),
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
