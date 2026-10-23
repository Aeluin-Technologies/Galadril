import { stageFrontend } from "../../testing/stage.mjs";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { readdirSync, readFileSync } from "node:fs";

const stage = stageFrontend(resolve(process.cwd(), ".."), "ui");
process.on("exit", stage.cleanup);
const require = createRequire(resolve(stage.path, "package.json"));
process.chdir(stage.path);
const cli = resolve(
  dirname(require.resolve("nuxt/package.json")),
  "bin/nuxt.mjs",
);
const result = spawnSync(process.execPath, [cli, "build", "playground"], {
  env: { ...process.env, NUXT_TELEMETRY_DISABLED: "1" },
  stdio: "inherit",
  timeout: 180_000,
});
assert.equal(result.status, 0, "Nuxt consumer production build failed");
const typecheck = spawnSync(
  process.execPath,
  [
    resolve(dirname(require.resolve("vue-tsc/package.json")), "bin/vue-tsc.js"),
    "--noEmit",
    "-p",
    "playground/.nuxt/tsconfig.app.json",
  ],
  {
    env: process.env,
    encoding: "utf8",
    timeout: 60_000,
  },
);
assert.equal(typecheck.status, 0, "Nuxt consumer type check failed");
process.stdout.write(typecheck.stdout ?? "");
process.stderr.write(typecheck.stderr ?? "");
assert.doesNotMatch(
  typecheck.stderr ?? "",
  /Resolve plugin path failed/,
  "Missing Vue language plugin",
);
const output = resolve("playground/.output/public/_nuxt");
const styles = readdirSync(output).filter((name) => name.endsWith(".css"));
assert.ok(styles.length > 0, "Missing production CSS");
const css = styles
  .map((name) => readFileSync(resolve(output, name), "utf8"))
  .join("\n");
assert.match(css, /--ui-primary/);
assert.match(css, /\.bg-primary/);
assert.match(css, /prefers-reduced-motion/);
process.stdout.write(
  `Production consumer: ${styles.length} stylesheets, ${Buffer.byteLength(css)} CSS bytes.\n`,
);
