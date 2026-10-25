import { required } from "./support.ts";
import assert from "node:assert/strict";
import test from "node:test";
import { testEvent } from "./nitro-fixture.ts";
import { record } from "../shared/records.ts";
import type { EventHandler, H3Error } from "h3";
import { readFile } from "node:fs/promises";
import { sealSession, unsealSession } from "h3";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import * as activity from "../app/utils/session-activity.ts";
import { isDashboardDocument } from "../server/auth/policy.ts";

test("renewal shell allowlisting excludes API, assets and serialized page data", async () => {
  const configuration = await readFile(
    new URL("../nuxt.config.ts", import.meta.url),
    "utf8",
  );
  assert.match(configuration, /\n {2}ssr: false,/);
  for (const path of [
    "/",
    "/builder",
    "/pipeline/new",
    "/settings/sso",
    "/pengolo",
    "/pengolo/conversation-1",
  ])
    assert.equal(isDashboardDocument(path), true, path);
  for (const path of [
    "/graphql",
    "/auth/session",
    "/_nuxt/app.js",
    "/settings/sso/_payload.json",
    "/pengolo/_payload.json",
  ])
    assert.equal(isDashboardDocument(path), false, path);
});

test("navigation restores a valid renewal once after access-token expiry", async () => {
  const calls: [string, RequestInit][] = [];
  const session = { subject: "alice", tenant: "demo" };
  const result = await activity.navigationSession(async (path, options) => {
    calls.push([path, options]);
    if (calls.length === 1) throw { statusCode: 401 };
    return path === "/auth/refresh" ? { expiresAt: 8000 } : session;
  });
  assert.equal(result, session);
  assert.deepEqual(
    calls.map(([path]) => path),
    ["/auth/session", "/auth/refresh", "/auth/session"],
  );
  assert.equal(required(calls[1])[1].method, "POST");
  assert.ok(
    calls.every(([, options]) => options.credentials === "same-origin"),
  );
  for (const statusCode of [401, 428, 503]) {
    let attempts = 0;
    await assert.rejects(
      activity.navigationSession(async (path) => {
        attempts += 1;
        if (path === "/auth/session") throw { statusCode: 401 };
        throw { statusCode };
      }),
      (error) => record(error).statusCode === statusCode,
    );
    assert.equal(attempts, 2, "Failed renewal must never loop");
  }
  let attempts = 0;
  await assert.rejects(
    activity.navigationSession(async () => {
      attempts += 1;
      throw { statusCode: 503 };
    }),
  );
  assert.equal(attempts, 1, "Provider outages must preserve the current page");
});

test("protected SPA documents can restore only authentic, active sealed sessions without GET mutations", async () => {
  const now = Date.now();
  const secret = "hermetic-renewal-cookie-test-secret-at-least-32";
  const name = "__Host-galadril-renewal";
  const state = {
    subject: "alice",
    tenant: "demo",
    refreshToken: "private-test-refresh-token",
    createdAt: now - 20 * 60_000,
    lastActiveAt: now - 16 * 60_000,
  };
  const seal = (data: Record<string, unknown>) => {
    const event = testEvent();
    event.context.sessions = {
      [name]: { id: "test-session", createdAt: now, data },
    };
    return sealSession(event, { name, password: secret, maxAge: 8 * 60 * 60 });
  };
  let renewal = await seal(state);
  let token: string | undefined;
  let path = "/settings/sso";
  let method: "GET" | "POST" = "GET";
  const environment = {
    AUTH_PUBLIC_ORIGIN: "https://app.example",
    AUTH_ISSUER: "https://issuer.example",
    AUTH_SESSION_SECRET: secret,
    AUTH_CLIENT_SECRET: "test-client-secret",
    AUTH_HYDRA_PUBLIC_URL: "https://issuer.example",
    AUTH_HYDRA_ADMIN_URL: "http://127.0.0.1:4445",
    AUTH_POLIS_URL: "http://127.0.0.1:5225",
    AUTH_POLIS_API_KEY: "test-polis-key",
    AUTH_JWKS_URL: "https://issuer.example/.well-known/jwks.json",
    AUTH_GATEWAY_URL: "http://127.0.0.1:8081/internal/auth/graphql",
    AUTH_GATEWAY_KEY: "test-private-gateway-key-at-least-32",
    AUTH_LOCAL_LOGIN_ENABLED: "false",
  };
  const originalEnvironment = new Map(
    Object.keys(environment).map((key) => [key, process.env[key]]),
  );
  const { privateKey, publicKey } = await generateKeyPair("RS256");
  const key = {
    ...(await exportJWK(publicKey)),
    kid: "restoration-key",
    alg: "RS256",
  };
  const expired = await new SignJWT({ tenant_id: "demo", token_use: "access" })
    .setProtectedHeader({ alg: "RS256", kid: key.kid })
    .setIssuer(environment.AUTH_ISSUER)
    .setAudience("galadril")
    .setSubject("alice")
    .setIssuedAt(Math.floor(now / 1000) - 1000)
    .setExpirationTime(Math.floor(now / 1000) - 1)
    .sign(privateKey);
  const replacements = {
    defineEventHandler: (handler: EventHandler) => handler,
    getRequestURL: () => new URL(path, "https://app.example"),
    setResponseHeader() {},
    getCookie: (_event: unknown, cookie: string) =>
      cookie === name ? renewal : token,
    getHeader: () => "text/html",
    createError: (details: Partial<H3Error>) =>
      Object.assign(new Error("Request failed"), details),
    sendRedirect: () => "redirected",
    unsealSession,
    setCookie() {
      assert.fail("A shell GET must not publish credentials");
    },
    useSession() {
      assert.fail("A shell GET must not create or update sessions");
    },
    fetch: async () =>
      new Response(JSON.stringify({ keys: [key] }), {
        headers: { "content-type": "application/json" },
      }),
  };
  const originals = new Map(
    Object.keys(replacements).map((key) => [key, Reflect.get(globalThis, key)]),
  );
  try {
    Object.assign(process.env, environment);
    Object.assign(globalThis, replacements);
    const { default: middleware } =
      await import("../server/middleware/auth.ts");
    assert.equal(await middleware(testEvent(method)), undefined);
    token = expired;
    assert.equal(await middleware(testEvent(method)), undefined);
    token = undefined;
    renewal = await seal({ ...state, lastActiveAt: now - 31 * 60_000 });
    assert.equal(await middleware(testEvent(method)), "redirected");
    renewal = "malformed";
    assert.equal(await middleware(testEvent(method)), "redirected");
    renewal = await seal(state);
    token = expired.slice(0, -3) + "bad";
    assert.equal(await middleware(testEvent(method)), "redirected");
    token = undefined;
    path = "/graphql";
    assert.equal(await middleware(testEvent(method)), "redirected");
    path = "/settings/sso";
    method = "POST";
    assert.equal(await middleware(testEvent(method)), "redirected");
  } finally {
    for (const [key, value] of originals) {
      if (value === undefined) Reflect.deleteProperty(globalThis, key);
      else Reflect.set(globalThis, key, value);
    }
    for (const [key, value] of originalEnvironment) {
      if (value === undefined) Reflect.deleteProperty(process.env, key);
      else process.env[key] = value;
    }
  }
});
