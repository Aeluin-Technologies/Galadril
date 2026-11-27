import assert from "node:assert/strict";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { record } from "../shared/records.ts";
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
  const { ResolverFactory } = record(require("unrs-resolver"));
  assert.equal(typeof ResolverFactory, "function");
  if (typeof ResolverFactory !== "function")
    throw new Error("Missing resolver constructor");
  const resolver = record(Reflect.construct(ResolverFactory, []));
  const resolveEntry = (mode: "sync" | "async", directory: string): unknown => {
    const method = resolver[mode];
    if (typeof method !== "function")
      throw new Error("Missing resolver method");
    return Reflect.apply(method, resolver, [directory, "./entry.js"]);
  };
  const directory = mkdtempSync(join(tmpdir(), "galadril-native-resolver-"));
  try {
    const fixture = join(directory, "entry.js");
    writeFileSync(fixture, "export default true;\n");
    const expected = realpathSync(fixture);
    assert.equal(record(resolveEntry("sync", directory)).path, expected);
    assert.equal(record(await resolveEntry("async", directory)).path, expected);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
