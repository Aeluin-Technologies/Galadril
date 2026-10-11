import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import { GatewayAuthClient } from "../server/auth/gateway.mjs";
import { requestJson } from "../server/auth/http.mjs";
import { GatewayPasskeyStore } from "../server/auth/passkey-store.mjs";
import {
  AggregationTemporality,
  InMemoryMetricExporter,
} from "@opentelemetry/sdk-metrics";

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = await Promise.all(
    entries.map((entry) => {
      const path = new URL(
        entry.name + (entry.isDirectory() ? "/" : ""),
        directory,
      );
      return entry.isDirectory() ? sourceFiles(path) : [path];
    }),
  );
  return files.flat();
}

test("authentication transport rejects malformed service destinations before fetching", async () => {
  const original = globalThis.fetch;
  let calls = 0;
  try {
    globalThis.fetch = async () => {
      calls += 1;
      return new Response("{}");
    };
    for (const url of [
      new URL("file:///etc/passwd"),
      new URL("ftp://service.example/secret"),
      new URL("https://user:secret@service.example/"),
      new URL("https://service.example/#fragment"),
      "https://service.example/",
    ])
      await assert.rejects(requestJson(url), /Invalid authentication endpoint/);
    assert.equal(calls, 0);
    await requestJson(new URL("http://127.0.0.1:8081/internal/auth/graphql"));
    await requestJson(new URL("https://service.example/sessions/whoami"));
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = original;
  }
});

test("Nuxt has no database driver, connection setting or SQL implementation", async () => {
  const pkg = JSON.parse(
    await readFile(new URL("../package.json", import.meta.url), "utf8"),
  );
  assert.equal(pkg.dependencies.pg, undefined);
  for (const path of await sourceFiles(
    new URL("../server/", import.meta.url),
  )) {
    const source = await readFile(path, "utf8");
    assert.doesNotMatch(
      source,
      /AUTH_DATABASE_URL|from ["'](?:pg|postgres|sqlx)["']|\b(?:SELECT|INSERT INTO|UPDATE|DELETE FROM|BEGIN|COMMIT|ROLLBACK)\b/,
      path.pathname,
    );
  }
});

test("private GraphQL uses variables and rejects redirects, upstream errors and unknown operations", async () => {
  const original = globalThis.fetch;
  const client = new GatewayAuthClient(
    "http://127.0.0.1:8081/internal/auth/graphql",
    "private-test-key-at-least-32-characters",
  );
  try {
    globalThis.fetch = async (url, options) => {
      assert.equal(url.pathname, "/internal/auth/graphql");
      assert.equal(options.redirect, "error");
      assert.equal(options.headers["x-galadril-auth-key"], client.key);
      const payload = JSON.parse(options.body);
      assert.equal(payload.variables.user, "alice");
      assert.match(payload.query, /activeUser/);
      return new Response(JSON.stringify({ data: { activeUser: "alice" } }));
    };
    assert.equal(
      await client.request("activeUser", { tenant: "demo", user: "alice" }),
      "alice",
    );
    await assert.rejects(client.request("arbitrarySql", {}));
    globalThis.fetch = async () =>
      new Response(
        JSON.stringify({ errors: [{ message: "secret database details" }] }),
      );
    await assert.rejects(
      client.request("activeUser", { tenant: "demo", user: "alice" }),
      { message: "Authentication unavailable or denied" },
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("WebAuthn binary material and counter preconditions survive the GraphQL boundary", async () => {
  const calls = [];
  const store = new GatewayPasskeyStore({
    async request(operation, variables) {
      calls.push({ operation, variables });
      if (operation === "passkeyContext")
        return JSON.stringify({
          enabled: true,
          generation: 3,
          userHandle: "AQI",
          credentials: [{ id: "key", publicKey: "AwQ", counter: 7 }],
        });
      return true;
    },
  });
  const context = await store.context("demo", "alice");
  assert.deepEqual([...context.userHandle], [1, 2]);
  assert.deepEqual([...context.credentials[0].publicKey], [3, 4]);
  await store.save(
    "demo",
    "alice",
    3,
    {
      id: "key",
      publicKey: new Uint8Array([3, 4]),
      counter: 8,
      deviceType: "singleDevice",
      backedUp: false,
    },
    7,
  );
  assert.equal(calls[1].variables.previousCounter, "7");
  assert.equal(JSON.parse(calls[1].variables.credential).publicKey, "AwQ");
});

test("Nuxt telemetry exports only explicitly enabled aggregate metrics", async () => {
  const config = await readFile(
    new URL("../nuxt.config.ts", import.meta.url),
    "utf8",
  );
  const plugin = await readFile(
    new URL("../server/plugins/telemetry.ts", import.meta.url),
    "utf8",
  );
  assert.match(config, /telemetry:\s*false/);
  assert.match(plugin, /NUXT_AGGREGATE_METRICS_ENABLED/);
  assert.doesNotMatch(plugin, /TraceExporter|NodeSDK|instrumentations/);
  assert.match(plugin, /createAggregateMetricsProvider/);
});

test("aggregate exports remove identities and discard unapproved instruments", async () => {
  const { createAggregateMetricsProvider } =
    await import("../server/auth/metrics.mjs");
  const exporter = new InMemoryMetricExporter(
    AggregationTemporality.CUMULATIVE,
  );
  const provider = createAggregateMetricsProvider(exporter);
  try {
    const meter = provider.getMeter("privacy-test");
    const attempts = meter.createCounter("galadril.auth.attempts");
    for (const email of ["alice@example.com", "bob@example.com"]) {
      attempts.add(1, {
        operation: "login",
        outcome: "success",
        email,
        tenant: email,
        token: "private-session-token",
      });
    }
    meter.createCounter("individual.requests").add(1, { user: "alice" });
    await provider.forceFlush();
    const exported = exporter.getMetrics();
    assert.equal(exported.length, 1);
    assert.deepEqual(exported[0].resource.attributes, {
      "service.name": "galadril-studio",
    });
    const instruments = exported.flatMap((resource) =>
      resource.scopeMetrics.flatMap((scope) => scope.metrics),
    );
    assert.equal(instruments.length, 1);
    assert.equal(instruments[0].descriptor.name, "galadril.auth.attempts");
    assert.equal(instruments[0].dataPoints.length, 1);
    assert.equal(instruments[0].dataPoints[0].value, 2);
    assert.deepEqual(instruments[0].dataPoints[0].attributes, {
      operation: "login",
      outcome: "success",
    });
    assert.doesNotMatch(
      JSON.stringify(exported),
      /alice|bob|example\.com|private-session-token/,
    );
  } finally {
    await provider.shutdown();
  }
});
