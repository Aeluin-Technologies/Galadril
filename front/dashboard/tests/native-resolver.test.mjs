import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { test } from "node:test";

test("Nuxt ESLint resolves imports with its packaged native binding", async () => {
  let require = createRequire(import.meta.url);
  for (const dependency of [
    "@nuxt/eslint",
    "@nuxt/eslint-config",
    "eslint-plugin-import-x",
    "eslint-import-context",
    "unrs-resolver",
  ]) {
    require = createRequire(require.resolve(dependency));
  }
  const { ResolverFactory } = require("unrs-resolver");
  const resolver = new ResolverFactory();
  const directory = mkdtempSync(join(tmpdir(), "galadril-native-resolver-"));
  try {
    const fixture = join(directory, "entry.js");
    writeFileSync(fixture, "export default true;\n");
    const expected = realpathSync(fixture);
    assert.equal(resolver.sync(directory, "./entry.js").path, expected);
    assert.equal(
      (await resolver.async(directory, "./entry.js")).path,
      expected,
    );
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
