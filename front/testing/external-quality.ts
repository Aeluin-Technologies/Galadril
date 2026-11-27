import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  cpSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { createRequire } from "node:module";

const root = resolve(process.cwd());
const workspace = mkdtempSync(
  join(process.env.TEST_TMPDIR ?? tmpdir(), "galadril-typescript-"),
);
process.on("exit", () => rmSync(workspace, { recursive: true, force: true }));

for (const path of [
  "infrastructure/envoy",
  "tests/e2e/dashboard",
  "front/testing",
]) {
  cpSync(join(root, path), join(workspace, path), {
    recursive: true,
    dereference: true,
  });
}
symlinkSync(
  join(root, "front/ui/node_modules"),
  join(workspace, "node_modules"),
);
symlinkSync(
  join(root, "front/node_modules"),
  join(workspace, "front/node_modules"),
);
writeFileSync(
  join(workspace, "tsconfig.json"),
  JSON.stringify({
    compilerOptions: {
      strict: true,
      noUncheckedIndexedAccess: true,
      noEmit: true,
      target: "ES2024",
      module: "ESNext",
      moduleResolution: "Bundler",
      skipLibCheck: true,
      allowImportingTsExtensions: true,
      types: ["node"],
    },
    include: [
      "infrastructure/**/*.ts",
      "tests/**/*.ts",
      "front/testing/**/*.ts",
    ],
  }),
);

const require = createRequire(join(workspace, "package.json"));
const checks = [
  [
    "eslint",
    "bin/eslint.js",
    [
      "--config",
      "front/testing/external-eslint.config.ts",
      "infrastructure",
      "tests",
      "front",
      "--max-warnings",
      "0",
    ],
  ],
  [
    "prettier",
    "bin/prettier.cjs",
    ["--check", "{front,infrastructure,tests}/**/*.ts"],
  ],
  ["typescript", "bin/tsc", ["--project", "tsconfig.json"]],
] as const;
let failed = false;

for (const [tool, entry, args] of checks) {
  const packageDir = dirname(require.resolve(`${tool}/package.json`));
  const result = spawnSync(
    process.execPath,
    [join(packageDir, entry), ...args],
    {
      cwd: workspace,
      stdio: "inherit",
    },
  );

  if (result.error) throw result.error;
  failed ||= result.status !== 0;
}
assert.equal(failed, false, "Infrastructure TypeScript quality checks failed");
