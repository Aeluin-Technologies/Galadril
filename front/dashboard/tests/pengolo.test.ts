import assert from "node:assert/strict";
import { test } from "node:test";
import {
  createRequestCache,
  mergeEvents,
  replayStatus,
  compatibleModels,
  newDraft,
  invalidatesWorkspace,
  mergeMessages,
  unavailableActiveChat,
} from "../app/utils/pengolo.ts";

test("an unavailable active chat cannot prevent a freshly authorized workspace from loading", () => {
  assert.equal(
    unavailableActiveChat([
      { path: ["activeChat"], extensions: { code: "REQUEST_FAILED" } },
    ]),
    true,
  );
  assert.equal(
    unavailableActiveChat([
      { path: ["pengoloProjects"], extensions: { code: "FORBIDDEN" } },
    ]),
    false,
  );
  assert.equal(
    unavailableActiveChat([
      { path: ["activeChat"], extensions: { code: "REQUEST_FAILED" } },
      { path: ["pengoloIdentity"], extensions: { code: "FORBIDDEN" } },
    ]),
    false,
  );
  assert.equal(unavailableActiveChat([]), false);
});

test("older pages preserve attachments and terminal deltas replace messages without fetching history", () => {
  const recent = [{ id: "recent", text: "new" }];
  const older = [
    { id: "old", text: "old", fileIds: ["document"] },
    { id: "recent", text: "stale" },
  ];
  assert.deepEqual(mergeMessages(recent, older, true), [older[0], recent[0]]);
  assert.deepEqual(mergeMessages(recent, [{ id: "recent", text: "final" }]), [
    { id: "recent", text: "final" },
  ]);
});

test("an ordinary validation or provider failure cannot erase the active workspace", () => {
  assert.equal(
    invalidatesWorkspace([{ extensions: { code: "REQUEST_FAILED" } }]),
    false,
  );
  assert.equal(
    invalidatesWorkspace([{ message: "Temporary upstream outage" }]),
    false,
  );
  assert.equal(
    invalidatesWorkspace([{ extensions: { code: "FORBIDDEN" } }]),
    true,
  );
  assert.equal(
    invalidatesWorkspace([{ extensions: { code: "AUTHENTICATION_EXPIRED" } }]),
    true,
  );
});

test("new chat is a draft without a durable identifier", () => {
  for (let index = 0; index < 10; index++) {
    assert.equal(newDraft().chatId, null);
    assert.deepEqual(newDraft().files, []);
  }
});

test("catalog preserves dynamic aliases and excludes models without medium", () => {
  const available = compatibleModels({
    models: [
      { alias: "configured-cloud", reasoningEfforts: ["low", "medium"] },
      { alias: "configured-local", reasoningEfforts: ["medium"] },
      { alias: "unsupported", reasoningEfforts: [] },
    ],
  });
  assert.deepEqual(
    available.map((model) => model.alias),
    ["configured-cloud", "configured-local"],
  );
  assert.deepEqual(compatibleModels({ models: [] }), []);
});

test("replay ignores duplicates and rejects missing events", () => {
  const events = [{ sequence: 1, kind: "content", content: "Hello" }];
  const replay = mergeEvents(events, [
    events[0],
    { sequence: 2, kind: "completed", content: "" },
  ]);
  assert.equal(replay.length, 2);
  assert.throws(() =>
    mergeEvents(events, [{ sequence: 3, kind: "content", content: "lost" }]),
  );
});

for (const status of ["completed", "failed", "cancelled"]) {
  test(`durable ${status} overrides an older running snapshot`, () => {
    assert.equal(
      replayStatus("running", "running", [
        { sequence: 1, kind: "content", content: "answer" },
        { sequence: 2, kind: status, content: "" },
      ]),
      status,
    );
  });

  test(`${status} cannot regress after a delayed replay response`, () => {
    assert.equal(replayStatus(status, "running", []), status);
  });
}

test("content alone does not finish a generation", () => {
  assert.equal(
    replayStatus("pending", "running", [
      { sequence: 1, kind: "content", content: "answer" },
    ]),
    "running",
  );
});

test("cache deduplicates requests and clearing defeats in-flight stale results", async () => {
  let count = 0;
  const pending = Promise.withResolvers<string>();
  const cache = createRequestCache<string>();
  const fetch = () => {
    count++;
    return pending.promise;
  };
  const first = cache.get("models", fetch);
  const second = cache.get("models", fetch);
  assert.equal(count, 1);
  cache.clear();
  pending.resolve("old-tenant-models");
  assert.deepEqual(
    (await Promise.allSettled([first, second])).map((result) => result.status),
    ["rejected", "rejected"],
  );
  assert.equal(
    await cache.get("models", async () => "new-tenant-models"),
    "new-tenant-models",
  );
});

test("cache invalidation rejects a cached value already queued for delivery", async () => {
  const cache = createRequestCache<string>();
  await cache.get("private", async () => "previous actor content");
  const queued = cache.get("private", async () => "unexpected reload");
  cache.clear();
  await assert.rejects(queued, /Workspace identity changed/);
  assert.equal(
    await cache.get("private", async () => "current actor content"),
    "current actor content",
  );
});
