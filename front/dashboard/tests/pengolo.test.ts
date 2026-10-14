import assert from "node:assert/strict";
import { test } from "node:test";
import {
  createRequestCache,
  mergeEvents,
  compatibleModels,
  newDraft,
  publicWebQuery,
} from "../app/utils/pengolo.ts";

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

test("web search requires a separate explicit public query", () => {
  assert.throws(() => publicWebQuery(true, ""));
  assert.equal(publicWebQuery(false, ""), null);
  assert.equal(
    publicWebQuery(true, "public inflation statistics"),
    "public inflation statistics",
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
