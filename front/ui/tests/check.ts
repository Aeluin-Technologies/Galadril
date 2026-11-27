import { spawn, spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { readFile, readdir, stat } from "node:fs/promises";

import { stageFrontend } from "../../testing/stage.ts";

const stage = stageFrontend(resolve(process.cwd(), ".."), "ui");
process.on("exit", stage.cleanup);
const require = createRequire(resolve(stage.path, "package.json"));
process.chdir(stage.path);
if (process.argv[2] === "format") {
  const prettier = await import("prettier");
  async function verify(path: string): Promise<void> {
    if ((await stat(path)).isDirectory()) {
      for (const name of await readdir(path)) await verify(resolve(path, name));
      return;
    }
    const options = await prettier.resolveConfig(path);
    if (
      !(await prettier.check(await readFile(path, "utf8"), {
        ...options,
        filepath: path,
      }))
    ) {
      process.stderr.write(`Formatting required: ${path}\n`);
      process.exitCode = 1;
    }
  }
  for (const path of [
    "src",
    "tests",
    "playground/app",
    "playground/nuxt.config.ts",
    "tsconfig.json",
    "package.json",
    "eslint.config.ts",
  ])
    await verify(resolve(path));
} else {
  const prepare = spawnSync(
    process.execPath,
    [
      resolve(dirname(require.resolve("nuxt/package.json")), "bin/nuxt.mjs"),
      "prepare",
      "playground",
    ],
    {
      stdio: "inherit",
      env: { ...process.env, NUXT_TELEMETRY_DISABLED: "1" },
    },
  );
  if (prepare.error) throw prepare.error;
  if (prepare.status !== 0) process.exit(prepare.status ?? 1);

  const checks: Record<string, string[]> = {
    typecheck: [
      resolve(
        dirname(require.resolve("vue-tsc/package.json")),
        "bin/vue-tsc.js",
      ),
      "--noEmit",
      "-p",
      "tsconfig.json",
    ],
    lint: [
      resolve(dirname(require.resolve("eslint/package.json")), "bin/eslint.js"),
      "**/*.{ts,tsx,mts,cts,vue}",
      "--ignore-pattern",
      "**/.nuxt/**",
      "--ignore-pattern",
      "**/.output/**",
      "--max-warnings",
      "0",
    ],
  };
  const args = checks[process.argv[2] ?? ""];
  if (!args) throw new Error("Unknown quality check");
  const runner = spawn(process.execPath, args, {
    stdio: "inherit",
    env: process.env,
  });
  runner.on("error", () => {
    process.exitCode = 1;
  });
  runner.on("exit", (code) => {
    process.exitCode = code ?? 1;
  });
}
