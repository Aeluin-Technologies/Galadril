import assert from "node:assert/strict";
import { test } from "node:test";
import { librarySnapshot } from "../app/utils/pengolo-library.ts";

test("invalidation refreshes all loaded pages, including insertions and deletions", async () => {
  const cursors: Array<string | null> = [];
  const page = await librarySnapshot(
    2,
    async (after) => {
      cursors.push(after);
      return after === null
        ? { items: [{ id: "new" }, { id: "a" }], after: "a" }
        : { items: [{ id: "c" }], after: null };
    },
    () => true,
  );
  assert.deepEqual(cursors, [null, "a"]);
  assert.deepEqual(page, {
    items: [{ id: "new" }, { id: "a" }, { id: "c" }],
    after: null,
  });
});

test("an identity or scope change discards in-flight results before fetching another page", async () => {
  let current = true;
  let requests = 0;
  const page = await librarySnapshot(
    3,
    async () => {
      requests++;
      current = false;
      return { items: [{ id: "private" }], after: "private" };
    },
    () => current,
  );
  assert.equal(page, null);
  assert.equal(requests, 1);
});

test("overlapping cursor pages do not duplicate files", async () => {
  const page = await librarySnapshot(
    2,
    async (after) =>
      after === null
        ? { items: [{ id: "a" }], after: "a" }
        : { items: [{ id: "a" }, { id: "b" }], after: null },
    () => true,
  );
  assert.deepEqual(
    page?.items.map((file) => file.id),
    ["a", "b"],
  );
});
