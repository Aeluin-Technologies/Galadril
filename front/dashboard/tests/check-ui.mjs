import { stageFrontend } from "../../testing/stage.mjs";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";

const stage = stageFrontend(resolve(process.cwd(), ".."), "dashboard");
process.on("exit", stage.cleanup);
const require = createRequire(resolve(stage.path, "package.json"));
process.chdir(stage.path);
const environment = { ...process.env, NUXT_TELEMETRY_DISABLED: "1" };
function run(packageName, entry, args) {
  const result = spawnSync(
    process.execPath,
    [
      resolve(dirname(require.resolve(`${packageName}/package.json`)), entry),
      ...args,
    ],
    {
      stdio: "inherit",
      env: environment,
      timeout: 240_000,
    },
  );
  assert.equal(result.status, 0, `${packageName} ${args.join(" ")} failed`);
}
const mode = process.argv[2];
if (mode === "typecheck") {
  run("nuxt", "bin/nuxt.mjs", ["prepare"]);
  run("vue-tsc", "bin/vue-tsc.js", [
    "--noEmit",
    "-p",
    "tsconfig.dashboard.json",
  ]);
} else if (mode === "build") {
  run("nuxt", "bin/nuxt.mjs", ["build", "--envName", "ui-test"]);
} else {
  throw new Error("Unknown dashboard check");
}
