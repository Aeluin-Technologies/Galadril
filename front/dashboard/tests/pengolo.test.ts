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
  compatibleAgent,
  documentScore,
  chatgptAuthorizationUrl,
  indexingState,
} from "../app/utils/pengolo.ts";

test("index progress distinguishes active, partial, completed and invalid projections", () => {
  assert.equal(indexingState({}).percent, 0);
  assert.equal(
    indexingState({ indexing: { stage: "embedding", percent: 65 } }).label,
    "Computing embeddings",
  );
  assert.equal(
    indexingState({ indexing: { stage: "embedding", percent: 65 } }).active,
    true,
  );
  assert.equal(
    indexingState({ retrieval: { status: "ready", warnings: [] } }).percent,
    100,
  );
  assert.equal(
    indexingState({
      retrieval: { status: "ready", warnings: ["enrichment_pending"] },
    }).active,
    true,
  );
  assert.equal(
    indexingState({ indexing: { stage: "failed", percent: 35 } }).active,
    false,
  );
  assert.equal(
    indexingState({
      indexing: { stage: "retrying", percent: 65 },
      retrieval: { status: "ready", warnings: [] },
    }).percent,
    65,
  );
  assert.equal(
    indexingState({ indexing: { stage: "unknown", percent: 500 } }).percent,
    0,
  );
});

test("OAuth windows accept only the OpenAI authorization endpoint", () => {
  const valid =
    "https://auth.openai.com/api/accounts/authorize?response_type=code";
  assert.equal(chatgptAuthorizationUrl(valid), valid);
  for (const value of [
    "https://attacker.test/api/accounts/authorize?response_type=code",
    "https://auth.openai.com.attacker.test/api/accounts/authorize?response_type=code",
    "http://auth.openai.com/api/accounts/authorize?response_type=code",
    "https://user:secret@auth.openai.com/api/accounts/authorize?response_type=code",
    "javascript:alert(1)",
    valid + "#private",
    "https://auth.openai.com/other?response_type=code",
  ])
    assert.equal(chatgptAuthorizationUrl(value), null);
});

test("retrieval scores remain model similarities rather than correctness probabilities", () => {
  assert.equal(documentScore(0.734), "0.73");
  assert.equal(documentScore(-0.25), "-0.25");
  assert.equal(documentScore(null), null);
  assert.equal(documentScore(Number.NaN), null);
});

test("specialists remain opt-in and cannot escape project or Skill model restrictions", () => {
  const agent = { projectId: "legal", data: { skillIds: ["law.skill.watch"] } };
  const skills = [
    {
      id: "law.skill.watch",
      kind: "SKILL",
      execution: "REGISTERED",
      compatibility: { allowedModels: ["local"], requires: ["law.mcp.courts"] },
    },
    {
      id: "law.mcp.courts",
      kind: "MCP",
      execution: "REGISTERED",
      compatibility: { allowedModels: [], requires: [] },
    },
  ];
  assert.equal(compatibleAgent(agent, "legal", "local", skills), true);
  assert.equal(compatibleAgent(agent, null, "local", skills), false);
  assert.equal(compatibleAgent(agent, "other", "local", skills), false);
  assert.equal(compatibleAgent(agent, "legal", "other", skills), false);
  const nested = skills.map((skill) => ({
    ...skill,
    compatibility: { ...skill.compatibility },
  }));
  nested[1]!.compatibility.requires = ["law.algorithm.verify"];
  assert.equal(compatibleAgent(agent, "legal", "local", nested), false);
  assert.equal(
    compatibleAgent(agent, "legal", "local", skills.slice(0, 1)),
    false,
  );
  assert.equal(
    compatibleAgent(
      agent,
      "legal",
      "local",
      skills.map((skill) => ({ ...skill, execution: "DECLARED" })),
    ),
    false,
  );
});

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

test("request caches evict by bytes and recency, purge expiry and bound concurrent loads", async () => {
  let time = 0;
  const cache = createRequestCache<string>({
    maxEntries: 2,
    maxBytes: 256,
    maxPending: 1,
    now: () => time,
  });
  let loads = 0;
  const get = (key: string) =>
    cache.get(
      key,
      async () => {
        loads++;
        return key;
      },
      10,
    );
  await get("a");
  await get("b");
  await get("a");
  await get("c");
  assert.equal(cache.stats().entries, 2);
  await get("b");
  assert.equal(loads, 4);
  await cache.get("oversized", async () => "x".repeat(200));
  assert.ok(cache.stats().bytes <= 256);
  assert.equal(cache.stats().entries, 2);
  time = 11;
  await get("d");
  assert.equal(cache.stats().entries, 1);
  const pending = Promise.withResolvers<string>();
  const blocked = cache.get("pending", () => pending.promise);
  await assert.rejects(
    cache.get("other", async () => "other"),
    /pending/i,
  );
  cache.invalidate("pending");
  pending.resolve("stale");
  await assert.rejects(blocked, /invalidated/i);
  cache.clear();
  assert.deepEqual(cache.stats(), { entries: 0, bytes: 0, pending: 0 });
});

test("loaded conversation search tolerates typos without fetching or retaining entire histories", async () => {
  const { createConversationIndex } =
    await import("../app/utils/pengolo-search.ts");
  const chat = (id: string, title: string, text: string) => ({
    id: id.repeat(32),
    ownerId: "alice",
    projectId: null,
    expiresAt: 100,
    revision: 1,
    updatedAt: 1,
    data: {
      title,
      pinned: false,
      activeGeneration: null,
      messages: [{ id: "m1", role: "user" as const, text }],
    },
  });
  const legal = chat(
    "a",
    "Veille jurisprudence",
    "Le tribunal a communiqué les nouvelles décisions.",
  );
  const travel = chat(
    "b",
    "Voyage en Bretagne",
    "Préparer les billets de train.",
  );
  const index = createConversationIndex([legal, travel], null, { now: 0 });
  assert.equal(index.search("jurisprudnce", 0)[0]?.chatId, legal.id);
  const message = index.search("tribnal", 0)[0];
  assert.equal(message?.kind, "message");
  assert.equal(message?.href, `/pengolo/${legal.id}#p-message-m1`);
  assert.equal(index.search("zzzzquux", 0).length, 0);
  assert.equal(index.search("tribunal", 101).length, 0);
  assert.deepEqual(index.search("a".repeat(513), 0), []);
  assert.deepEqual(
    createConversationIndex([], legal, { temporary: true, now: 0 }).search(
      "tribunal",
      0,
    ),
    [],
  );
  const longText = "x".repeat(20_000);
  const longMessages = Array.from({ length: 100 }, (_, message) => ({
    id: String(message),
    role: "user" as const,
    text: longText,
  }));
  const huge = createConversationIndex(
    Array.from({ length: 1000 }, (_, ordinal) => ({
      ...legal,
      id: ordinal.toString(16).padStart(32, "0"),
      data: { ...legal.data, messages: longMessages },
    })),
    null,
    { now: 0 },
  );
  assert.ok(huge.stats.entries <= 192);
  assert.ok(huge.stats.textBytes <= 128 * 1024);
  assert.ok(huge.search("veille", 0).length <= 12);
  const inaccessible = Array.from({ length: 1000 }, () => ({
    ...legal,
    expiresAt: -1,
  }));
  Object.defineProperty(inaccessible, "512", {
    get() {
      throw new Error("Source scan exceeded its budget");
    },
  });
  assert.equal(
    createConversationIndex(inaccessible, null, { now: 0 }).stats.entries,
    0,
  );
});

test("cached conversation typo benchmark preserves Recall@5 on 32 distinct topics", async (context) => {
  const { createConversationIndex } =
    await import("../app/utils/pengolo-search.ts");
  const topics = [
    "jurisprudence",
    "comptabilité",
    "calendrier",
    "recrutement",
    "patrimoine",
    "urbanisme",
    "formation",
    "facturation",
    "confidentialité",
    "architecture",
    "assurance",
    "transport",
    "énergie",
    "personnel",
    "messagerie",
    "trésorerie",
    "logistique",
    "procédure",
    "subvention",
    "informatique",
    "traduction",
    "matériaux",
    "innovation",
    "maintenance",
    "statistique",
    "ressources",
    "restauration",
    "bibliothèque",
    "publication",
    "sécurité",
    "production",
    "convention",
  ];
  const chats = topics.map((topic, index) => ({
    id: index.toString(16).padStart(32, "0"),
    ownerId: "alice",
    projectId: null,
    expiresAt: null,
    revision: 1,
    updatedAt: index,
    data: {
      title: topic,
      pinned: false,
      activeGeneration: null,
      messages: [
        {
          id: "evidence",
          role: "assistant" as const,
          text: `Les notes du dossier ${topic} sont disponibles.`,
        },
      ],
    },
  }));
  const began = performance.now();
  const index = createConversationIndex(chats, null);
  const indexMs = performance.now() - began;
  const elapsed: number[] = [];
  let found = 0;
  for (const [ordinal, topic] of topics.entries()) {
    const middle = Math.floor(topic.length / 2);
    const query = topic.slice(0, middle) + topic.slice(middle + 1);
    const started = performance.now();
    const hits = index.search(query).slice(0, 5);
    elapsed.push(performance.now() - started);
    if (hits.some((hit) => hit.chatId === chats.at(ordinal)?.id)) found++;
  }
  elapsed.sort((left, right) => left - right);
  context.diagnostic(
    JSON.stringify({
      queries: topics.length,
      recallAt5: found / topics.length,
      indexMs,
      p50Ms: elapsed.at(15),
      p95Ms: elapsed.at(30),
      indexedTextBytes: index.stats.textBytes,
    }),
  );
  assert.ok(
    found / topics.length >= 0.95,
    `Recall@5: ${found}/${topics.length}`,
  );
});

test("a refused extraction credential is actionable without hiding the searchable document", () => {
  const state = indexingState({
    indexing: { stage: "failed", percent: 95 },
    retrieval: {
      status: "ready",
      warnings: [
        "graph_extraction_unavailable",
        "graph_extraction_unauthorized",
      ],
    },
  });
  assert.equal(state.active, false);
  assert.equal(state.reasonKey, "pengolo.copy.indexing_credentials_refused");
  assert.equal(
    indexingState({ indexing: { stage: "extracting", percent: 95 } }).reasonKey,
    undefined,
  );
});
