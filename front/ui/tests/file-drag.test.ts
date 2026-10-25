import assert from "node:assert/strict";
import test from "node:test";
import { FileDragSession } from "../src/file-drag.ts";

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
