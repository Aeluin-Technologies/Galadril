import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { FileDragSession } from "../src/file-drag.ts";

test("shared UI type checking accepts the explicit TypeScript imports used by Node tests", () => {
  const configuration: unknown = JSON.parse(
    readFileSync(new URL("../tsconfig.json", import.meta.url), "utf8"),
  );
  assert.ok(configuration && typeof configuration === "object");
  assert.ok("compilerOptions" in configuration);
  const options = configuration.compilerOptions;
  assert.ok(options && typeof options === "object");
  assert.ok("allowImportingTsExtensions" in options);
  assert.equal(options.allowImportingTsExtensions, true);
});

test("file-drop feedback survives child transitions and ignores dragged text or links", () => {
  const session = new FileDragSession();
  assert.equal(session.enter(["text/plain", "text/uri-list"]), false);
  assert.equal(session.enter(["Files"]), true);
  assert.equal(session.enter([]), true);
  assert.equal(session.leave(), true);
  assert.equal(session.leave(), false);
  assert.equal(session.leave(), false);
  session.enter(["Files"]);
  session.reset();
  assert.equal(session.active, false);
});
