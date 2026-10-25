import { stageFrontend } from "../../testing/stage.ts";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";

const stage = stageFrontend(resolve(process.cwd(), ".."), "dashboard");
process.on("exit", stage.cleanup);
const require = createRequire(resolve(stage.path, "package.json"));
process.chdir(stage.path);
const environment = { ...process.env, NUXT_TELEMETRY_DISABLED: "1" };
function run(packageName: string, entry: string, args: string[]) {
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
if (mode === "typecheck" || mode === "tools") {
  run("nuxt", "bin/nuxt.mjs", ["prepare"]);
  run("vue-tsc", "bin/vue-tsc.js", [
    "--noEmit",
    "-p",
    mode === "tools" ? "../tsconfig.tools.json" : "tsconfig.dashboard.json",
  ]);
} else if (mode === "lint") {
  run("nuxt", "bin/nuxt.mjs", ["prepare"]);
  run("eslint", "bin/eslint.js", [
    "app/utils",
    "server/auth",
    "server/media",
    "shared",
    "tests",
    "--max-warnings",
    "0",
  ]);
} else if (mode === "format") {
  run("prettier", "bin/prettier.cjs", [
    "app/utils",
    "server/auth",
    "server/media",
    "shared",
    "tests",
    "package.json",
    "eslint.config.ts",
    "tsconfig.dashboard.json",
    "tsconfig.pengolo.json",
    "tsconfig.json",
    "--check",
  ]);
} else if (mode === "build") {
  run("nuxt", "bin/nuxt.mjs", ["build", "--envName", "ui-test"]);
} else {
  throw new Error("Unknown dashboard check");
}
