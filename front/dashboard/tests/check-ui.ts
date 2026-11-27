import { stageFrontend } from "../../testing/stage.ts";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { copyFileSync } from "node:fs";
import { dirname, resolve } from "node:path";

const stage = stageFrontend(resolve(process.cwd(), ".."), "dashboard");
process.on("exit", stage.cleanup);
const require = createRequire(resolve(stage.path, "package.json"));
process.chdir(stage.path);
const environment = { ...process.env, NUXT_TELEMETRY_DISABLED: "1" };
function invoke(
  packageName: string,
  entry: string,
  args: string[],
  capture = false,
) {
  const result = spawnSync(
    process.execPath,
    [
      resolve(dirname(require.resolve(`${packageName}/package.json`)), entry),
      ...args,
    ],
    {
      stdio: capture ? "pipe" : "inherit",
      encoding: "utf8",
      env: environment,
      timeout: 240_000,
    },
  );
  if (result.error) throw result.error;
  return result;
}
function run(packageName: string, entry: string, args: string[]) {
  const result = invoke(packageName, entry, args);
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
} else if (mode === "policy") {
  run("nuxt", "bin/nuxt.mjs", ["prepare"]);
  const fixture = "app/utils/quality-fixture.ts";
  copyFileSync("tests/quality-fixture.ts.txt", fixture);
  const lint = invoke("eslint", "bin/eslint.js", [fixture], true);
  assert.equal(lint.status, 1, lint.stderr);
  for (const rule of [
    "no-explicit-any",
    "no-unsafe-assignment",
    "ban-ts-comment",
  ]) {
    assert.match(lint.stdout, new RegExp(rule));
  }
  const format = invoke(
    "prettier",
    "bin/prettier.cjs",
    ["--check", fixture],
    true,
  );
  assert.equal(format.status, 1, format.stderr);
  assert.match(format.stderr, /quality-fixture/);
  const types = invoke(
    "vue-tsc",
    "bin/vue-tsc.js",
    ["--noEmit", "-p", "tsconfig.dashboard.json"],
    true,
  );
  assert.equal(types.status, 2, types.stderr);
  assert.match(types.stdout, /quality-fixture.*TS7006/);
} else if (mode === "lint") {
  run("nuxt", "bin/nuxt.mjs", ["prepare"]);
  run("eslint", "bin/eslint.js", [
    "**/*.{ts,tsx,mts,cts,vue}",
    "--ignore-pattern",
    ".nuxt/**",
    "--ignore-pattern",
    ".output/**",
    "--max-warnings",
    "0",
  ]);
} else if (mode === "format") {
  run("prettier", "bin/prettier.cjs", [
    "**/*.{ts,tsx,mts,cts,vue}",
    "**/tsconfig*.json",
    "package.json",
    "!**/.nuxt/**",
    "!**/.output/**",
    "--check",
  ]);
} else if (mode === "build") {
  run("nuxt", "bin/nuxt.mjs", ["build", "--envName", "ui-test"]);
} else {
  throw new Error("Unknown dashboard check");
}
