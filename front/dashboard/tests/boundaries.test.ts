import { required } from "./support.ts";
import assert from "node:assert/strict";
import { testEvent } from "./nitro-fixture.ts";
import { record } from "../shared/records.ts";
import type { EventHandler, H3Error } from "h3";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import { GatewayAuthClient } from "../server/auth/gateway.ts";
import {
  requestJson,
  AuthenticationUpstreamError,
} from "../server/auth/http.ts";
import { GatewayPasskeyStore } from "../server/auth/passkey-store.ts";
import {
  AggregationTemporality,
  InMemoryMetricExporter,
} from "@opentelemetry/sdk-metrics";

test("upstream outages remain distinguishable from invalid user credentials", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () =>
      new Response("private failure details", { status: 503 });
    await assert.rejects(
      requestJson(new URL("https://service.example/")),
      AuthenticationUpstreamError,
    );
    globalThis.fetch = async () =>
      new Response("private revocation details", { status: 400 });
    await assert.rejects(
      requestJson(new URL("https://service.example/oauth2/token")),
      (error) =>
        !(error instanceof AuthenticationUpstreamError) &&
        !String(record(error).message).includes("private"),
    );
    globalThis.fetch = async () => new Response("slow down", { status: 429 });
    await assert.rejects(
      requestJson(new URL("https://service.example/")),
      AuthenticationUpstreamError,
    );
  } finally {
    globalThis.fetch = original;
  }
});

test("protected HTML preserves the signed-in browser when identity verification is temporarily unavailable", async () => {
  const replacements = {
    defineEventHandler: (handler: EventHandler) => handler,
    getRequestURL: () => new URL("https://app.example/"),
    setResponseHeader() {},
    getCookie() {
      throw new AuthenticationUpstreamError();
    },
    getHeader: () => "text/html",
    createError: (details: Partial<H3Error>) =>
      Object.assign(new Error("Request failed"), details),
    sendRedirect() {
      assert.fail("A provider outage must not redirect to sign-in");
    },
  };
  const originals = new Map(
    Object.keys(replacements).map((name) => [
      name,
      Reflect.get(globalThis, name),
    ]),
  );
  try {
    Object.assign(globalThis, replacements);
    const { default: middleware } =
      await import("../server/middleware/auth.ts");
    await assert.rejects(
      middleware(testEvent()),
      (error) => record(error).statusCode === 503,
    );
  } finally {
    for (const [name, value] of originals) {
      if (value === undefined) Reflect.deleteProperty(globalThis, name);
      else Reflect.set(globalThis, name, value);
    }
  }
});

async function sourceFiles(directory: URL): Promise<URL[]> {
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

test("authentication transport stops oversized upstream streams before reading the entire body", async () => {
  const original = globalThis.fetch;
  let reads = 0;
  let cancelled = false;
  const body = new ReadableStream(
    {
      pull(controller) {
        reads += 1;
        if (reads > 8) controller.close();
        else controller.enqueue(new Uint8Array(32768));
      },
      cancel() {
        cancelled = true;
      },
    },
    { highWaterMark: 0 },
  );
  try {
    globalThis.fetch = async () => new Response(body);
    await assert.rejects(
      requestJson(new URL("https://service.example/")),
      /Authentication/,
    );
    assert.equal(cancelled, true);
    assert.ok(reads <= 3, "The body must stop at the 64 KiB byte budget");
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
      assert.ok(url instanceof URL);
      assert.ok(options);
      assert.equal(url.pathname, "/internal/auth/graphql");
      assert.equal(required(options).redirect, "error");
      assert.equal(
        new Headers(options.headers).get("x-galadril-auth-key"),
        client.key,
      );
      assert.equal(typeof options.body, "string");
      const payload = JSON.parse(String(options.body));
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

test("private GraphQL dependency errors preserve the session while identity denials remain definitive", async () => {
  const original = globalThis.fetch;
  const client = new GatewayAuthClient(
    "http://127.0.0.1:8081/internal/auth/graphql",
    "private-test-key-at-least-32-characters",
  );
  try {
    for (const code of [
      "AUTHENTICATION_UNAVAILABLE",
      "AUTHENTICATION_DENIED",
    ]) {
      globalThis.fetch = async () =>
        new Response(
          JSON.stringify({
            data: { activeUser: null },
            errors: [
              { message: "private storage details", extensions: { code } },
            ],
          }),
        );
      await assert.rejects(
        client.request("activeUser", { tenant: "demo", user: "alice" }),
        (error) =>
          error instanceof AuthenticationUpstreamError ===
            (code === "AUTHENTICATION_UNAVAILABLE") &&
          !String(record(error).message).includes("private"),
      );
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("WebAuthn binary material and counter preconditions survive the GraphQL boundary", async () => {
  const calls: { operation: string; variables: Record<string, unknown> }[] = [];
  const store = new GatewayPasskeyStore({
    async request(operation: string, variables: Record<string, unknown>) {
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
  assert.deepEqual([...required(context.credentials[0]).publicKey], [3, 4]);
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
  assert.equal(required(calls[1]).variables.previousCounter, "7");
  assert.equal(
    JSON.parse(String(required(calls[1]).variables.credential)).publicKey,
    "AwQ",
  );
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
    await import("../server/auth/metrics.ts");
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
    assert.deepEqual(required(exported[0]).resource.attributes, {
      "service.name": "galadril-studio",
    });
    const instruments = exported.flatMap((resource) =>
      resource.scopeMetrics.flatMap((scope) => scope.metrics),
    );
    assert.equal(instruments.length, 1);
    assert.equal(
      required(instruments[0]).descriptor.name,
      "galadril.auth.attempts",
    );
    assert.equal(required(instruments[0]).dataPoints.length, 1);
    assert.equal(required(required(instruments[0]).dataPoints[0]).value, 2);
    assert.deepEqual(
      required(required(instruments[0]).dataPoints[0]).attributes,
      {
        operation: "login",
        outcome: "success",
      },
    );
    assert.doesNotMatch(
      JSON.stringify(exported),
      /alice|bob|example\.com|private-session-token/,
    );
  } finally {
    await provider.shutdown();
  }
});

test("JWKS transport outages preserve authentication instead of rejecting a valid identity", async () => {
  const { trustedKeys, verifyAccessToken } =
    await import("../server/auth/tokens.ts");
  const { generateKeyPair, SignJWT } = await import("jose");
  const { privateKey } = await generateKeyPair("RS256");
  const token = await new SignJWT({ tenant_id: "tenant", token_use: "access" })
    .setProtectedHeader({ alg: "RS256", kid: "current" })
    .setSubject("alice")
    .setIssuer("https://auth.example/")
    .setAudience("galadril")
    .setIssuedAt()
    .setExpirationTime("15m")
    .sign(privateKey);
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => {
      throw new TypeError("fetch failed", {
        cause: Object.assign(new Error("certificate expired"), {
          code: "CERT_HAS_EXPIRED",
        }),
      });
    };
    await assert.rejects(
      verifyAccessToken(
        token,
        "https://auth.example/",
        trustedKeys("https://auth.example/keys"),
      ),
      AuthenticationUpstreamError,
    );
  } finally {
    globalThis.fetch = original;
  }
});
