import { required } from "./support.ts";
import assert from "node:assert/strict";
import test from "node:test";
import type { BrowserRequest } from "../app/utils/auth-navigation.ts";
import {
  isPublicRoute,
  isDashboardDocument,
  discoverConnection,
  requireOrigin,
  validateConsent,
  constantEqual,
  accessClaims,
  externalSubject,
  safeRedirect,
} from "../server/auth/policy.ts";
import "./passkey.test.ts";
import "./admin.test.ts";
import "./boundaries.test.ts";
import {
  accountInitials,
  administrationActions,
} from "../app/utils/account.ts";
import { readFile } from "node:fs/promises";
import { generateKeyPair, SignJWT, exportJWK } from "jose";
import { verifyAccessToken } from "../server/auth/tokens.ts";
import { clientDefinition } from "../server/auth/client.ts";
import { findConnection, findLoginProvider } from "../server/auth/directory.ts";
import { connectionCredential } from "../server/auth/http.ts";
import { localSessionIdentity, localIdentity } from "../server/auth/local.ts";
import {
  beginSignIn,
  completePasswordSignIn,
  requestPasskey,
  passkeyFailure,
} from "../app/utils/auth-navigation.ts";

import "./session.test.ts";
import "./restore.test.ts";

test("email settings support shell renewal without exposing mail or OAuth APIs", () => {
  assert.equal(isDashboardDocument("/settings/email"), true);
  assert.equal(isPublicRoute("/settings/email"), false);
  assert.equal(isDashboardDocument("/settings/email/_payload.json"), false);
  assert.equal(isDashboardDocument("/internal/utils/authorize"), false);
});

test("translation assets load before login without opening protected routes", () => {
  for (const path of [
    "/login",
    "/auth/session",
    "/_nuxt/app.js",
    "/healthz",
    "/_i18n/30382949/en/messages.json",
    "/_i18n/aB12_cd/en-US/messages.json",
  ])
    assert.equal(isPublicRoute(path), true, path);
  for (const path of [
    "/",
    "/settings/sso",
    "/graphql",
    "/_i18n",
    "/_i18n/admin",
    "/_i18n/hash/en/settings/sso",
    "/_i18n/../settings/sso",
    "/settings/_i18n/hash/en/messages.json",
    "/_i18n/hash/en/messages.json/private",
  ])
    assert.equal(isPublicRoute(path), false, path);
});

test("passkeys invoke the authenticator synchronously with the prepared challenge", async () => {
  const calls: unknown[] = [];
  const options = { challenge: "prepared-challenge" };
  const authenticators = {
    startAuthentication: (value: { optionsJSON: { challenge: string } }) => {
      calls.push(value);
      return Promise.resolve({ id: "credential" });
    },
    startRegistration: () => assert.fail("Unexpected registration"),
  };
  const response = requestPasskey(
    { operation: "authentication", options },
    authenticators,
  );
  assert.deepEqual(calls, [{ optionsJSON: options }]);
  assert.deepEqual(await response, { id: "credential" });
  await assert.rejects(requestPasskey(undefined, authenticators));
});

test("passkey errors distinguish browser failures without exposing raw messages", () => {
  const denied = passkeyFailure({ name: "NotAllowedError", message: "secret" });
  assert.equal(denied.reason, "not_allowed");
  assert.match(denied.message, /browser/);
  assert.doesNotMatch(denied.message, /secret|lost/i);
  assert.equal(passkeyFailure({ name: "SecurityError" }).reason, "security");
  assert.equal(
    passkeyFailure({ name: "NotSupportedError" }).reason,
    "unsupported",
  );
  assert.equal(
    passkeyFailure({ name: "untrusted", message: "secret" }).reason,
    "failed",
  );
});

test("password authentication can transition to the passkey gate without a document reload", async () => {
  assert.equal(
    await completePasswordSignIn(
      "https://app.example/identity/self-service/login?flow=123",
      new URLSearchParams(),
      "https://app.example",
      async () => ({ ok: true, url: "https://app.example/login?mfa=1" }),
    ),
    "/login?mfa=1",
  );
});

test("onboarding teleports stop when their cached page is deactivated", async () => {
  const source = await readFile(
    new URL("../app/components/Onboarding/Tooltip.vue", import.meta.url),
    "utf8",
  );
  assert.match(source, /onDeactivated\(/);
  assert.match(source, /onActivated\(/);
  assert.match(source, /pageActive\.value\s*&&/);
});

test("account initials handle names, email addresses and missing profiles", () => {
  assert.equal(accountInitials("Alice Martin"), "AM");
  assert.equal(accountInitials("alice.martin@example.com"), "AM");
  assert.equal(accountInitials("demo@example.com"), "DE");
  assert.equal(accountInitials(""), "?");
});

test("incorrect passwords return to the same flow for inline errors", async () => {
  const action = "https://app.example/identity/self-service/login?flow=123";
  assert.equal(
    await completePasswordSignIn(
      action,
      new URLSearchParams(),
      "https://app.example",
      async () => ({
        ok: false,
        status: 400,
        url: action,
      }),
    ),
    "/login?flow=123",
  );
  await assert.rejects(
    completePasswordSignIn(
      action,
      new URLSearchParams(),
      "https://app.example",
      async () => ({
        ok: false,
        status: 400,
        url: "https://foreign.example/identity/self-service/login?flow=123",
      }),
    ),
  );
});

test("administration commands are exposed only after Gateway authorization", () => {
  assert.deepEqual(administrationActions(false), []);
  assert.deepEqual(administrationActions(undefined), []);
  assert.deepEqual(
    administrationActions(true).map((item) => item.href),
    [
      "/settings/sso",
      "/settings/sso#users",
      "/settings/sso#domains",
      "/settings/sso#permissions",
    ],
  );
});

test("account navigation preserves the navbar and provides scrollable administration", async () => {
  const navbar = await readFile(
    new URL("../app/components/Navbar/Top.vue", import.meta.url),
    "utf8",
  );
  const settings = await readFile(
    new URL("../app/pages/settings/sso.vue", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(navbar, /Organization access|order-last/);
  assert.match(navbar, /<NavbarAccount/);
  assert.match(navbar, /data-tour="search"/);
  assert.match(navbar, /data-tour="copilot"/);
  assert.doesNotMatch(navbar, /addEventListener\("keydown"/);
  assert.match(settings, /overflow-y-auto/);
  assert.match(settings, /<UiButton/);
  assert.match(settings, /middleware:.*administrator/);
});

test("personal settings expose optional ChatGPT connection without an administrator role", async () => {
  const settings = await readFile(
    new URL("../app/pages/settings/user.vue", import.meta.url),
    "utf8",
  );
  const account = await readFile(
    new URL("../app/components/Navbar/Account.vue", import.meta.url),
    "utf8",
  );
  assert.equal(isPublicRoute("/settings/user"), false);
  assert.doesNotMatch(settings, /middleware:.*administrator/);
  assert.match(settings, /administration\.copy\.user_settings/);
  assert.match(settings, /<AiOpenaiAccount/);
  assert.match(settings, /<EmailWorkspace[^>]*\bembedded/);
  assert.match(settings, /<SettingsNavigation/);
  assert.match(settings, /id: "email"/);
  assert.match(settings, /id: "oauth"/);
  assert.match(settings, /route\.hash/);
  assert.match(settings, /v-if="activeSection === 'email'"/);
  assert.match(settings, /v-else[\s\S]*?<AiOpenaiAccount/);
  assert.match(account, /to="\/settings\/user"/);
  assert.doesNotMatch(account, /to="\/settings\/email"/);
});
test("administrative and personal settings share one section navigation and item", async () => {
  const source = (path: string) =>
    readFile(new URL(path, import.meta.url), "utf8");
  const [administration, personal, navigation, item] = await Promise.all([
    source("../app/pages/settings/sso.vue"),
    source("../app/pages/settings/user.vue"),
    source("../app/components/Settings/Navigation.vue"),
    source("../app/components/Settings/NavigationItem.vue"),
  ]);
  for (const page of [administration, personal]) {
    assert.match(page, /<SettingsNavigation/);
    assert.doesNotMatch(page, /v-for="section in sections"/);
  }
  assert.match(navigation, /<SettingsNavigationItem[\s\S]*?v-for=/);
  assert.match(item, /<UiNavigationLink[\s\S]*?variant="section"/);
  assert.match(item, /<NuxtLink/);
});

test("internal login follows same-origin Ory redirects without document navigation", async () => {
  const calls: [string, RequestInit][] = [];
  const request: BrowserRequest = async (url, options) => {
    calls.push([url, options]);
    assert.equal(options.mode, "same-origin");
    assert.equal(options.credentials, "same-origin");
    return calls.length === 1
      ? {
          ok: true,
          json: async () => ({
            local: true,
            redirect: "https://app.example/oauth2/auth?state=test",
          }),
        }
      : { ok: true, url: "https://app.example/login?flow=123" };
  };
  assert.deepEqual(
    await beginSignIn("alice@demo.invalid", "https://app.example", request),
    { path: "/login?flow=123", external: false },
  );
  const body = calls[0]?.[1].body;
  assert.ok(body instanceof URLSearchParams);
  assert.equal(body.get("email"), "alice@demo.invalid");
  assert.equal(calls.length, 2);
});

test("enterprise login hands navigation to the browser before contacting its IdP", async () => {
  let calls = 0;
  const request = async () => {
    calls++;
    return {
      ok: true,
      json: async () => ({
        local: false,
        redirect: "https://app.example/oauth2/auth?state=test",
      }),
    };
  };
  assert.deepEqual(
    await beginSignIn("alice@acme.example", "https://app.example", request),
    { path: "/oauth2/auth?state=test", external: true },
  );
  assert.equal(calls, 1);
});

test("password submission retains the Kratos method and CSRF token", async () => {
  const fields = new URLSearchParams({
    method: "password",
    csrf_token: "csrf",
    password: "secret",
  });
  const request: BrowserRequest = async (url, options) => {
    assert.equal(new URL(url).pathname, "/identity/self-service/login");
    assert.equal(options.body, fields);
    assert.equal(options.mode, "same-origin");
    return { ok: true, url: "https://app.example/" };
  };
  assert.equal(
    await completePasswordSignIn(
      "https://app.example/identity/self-service/login?flow=123",
      fields,
      "https://app.example",
      request,
    ),
    "/",
  );
});

test("animated login refuses foreign redirects, unexpected endpoints, and upstream errors", async () => {
  for (const redirect of [
    "https://evil.example/oauth2/auth",
    "https://app.example/graphql",
  ]) {
    await assert.rejects(
      beginSignIn("alice@demo.invalid", "https://app.example", async () => ({
        ok: true,
        json: async () => ({ local: true, redirect }),
      })),
    );
  }
  await assert.rejects(
    beginSignIn("alice@demo.invalid", "https://app.example", async () => ({
      ok: false,
    })),
  );
  await assert.rejects(
    completePasswordSignIn(
      "https://evil.example/login",
      new URLSearchParams(),
      "https://app.example",
      async () => {
        throw new Error("must not send credentials");
      },
    ),
  );
  await assert.rejects(
    completePasswordSignIn(
      "https://app.example/identity/self-service/login?flow=123",
      new URLSearchParams(),
      "https://app.example",
      async () => ({ ok: true, url: "https://evil.example/" }),
    ),
  );
});

test("login forms preserve their Origin without disclosing cross-origin referrers", async () => {
  const configuration = await readFile(
    new URL("../nuxt.config.ts", import.meta.url),
    "utf8",
  );
  const policy = configuration.match(/referrerPolicy:\s*"([^"]+)"/)?.[1];
  assert.equal(policy, "same-origin");
  requireOrigin("https://app.example", "https://app.example");
  assert.throws(() => requireOrigin("null", "https://app.example"));
});

test("strict security headers permit passkeys exclusively on the dashboard origin", async () => {
  const configuration = await readFile(
    new URL("../nuxt.config.ts", import.meta.url),
    "utf8",
  );
  for (const operation of ["get", "create"]) {
    assert.match(
      configuration,
      new RegExp(`"publickey-credentials-${operation}":\\s*\\["self"\\]`),
    );
  }
});

test("Kratos submit controls remain successful while the browser serializes the form", async () => {
  const page = await readFile(
    new URL("../app/pages/login.vue", import.meta.url),
    "utf8",
  );
  for (const form of page.matchAll(/<form\b[^>]*>[\s\S]*?<\/form>/g)) {
    if (/@submit="submitting = true"/.test(form[0])) {
      assert.doesNotMatch(
        form[0],
        /<(?:button|UtilsButton)\b(?=[^>]*:name=)(?=[^>]*:disabled="[^"]*submitting)[^>]*>/,
      );
    }
  }
});

test("login actions use the dashboard button component and retain native form submission", async () => {
  const page = await readFile(
    new URL("../app/pages/login.vue", import.meta.url),
    "utf8",
  );
  const forms = (
    await Promise.all(
      ["EmailForm", "PasswordForm", "PasskeyForm"].map((name) =>
        readFile(
          new URL(
            `../app/components/Authentication/${name}.vue`,
            import.meta.url,
          ),
          "utf8",
        ),
      ),
    )
  ).join("\n");
  const actions = [...forms.matchAll(/<UiButton\b[^>]*>/g)];
  assert.ok(actions.length >= 2);
  for (const action of actions) assert.match(action[0], /type="submit"/);
  assert.doesNotMatch(forms, /<button\b/);
  assert.match(forms, /:name="node.attributes.name"/);
  assert.match(forms, /:value="node.attributes.value"/);
  assert.match(forms, /action="\/auth\/start"/);
  assert.match(forms, /name="email"/);
  assert.doesNotMatch(
    page,
    /continuing|Sign in with SSO|test account|\/auth\/local\/start/,
  );
});

test("login decoration stays outside the accessible form and uses the amber palette", async () => {
  const page = await readFile(
    new URL("../app/pages/login.vue", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(page, /Workspace access/);
  const frame = await readFile(
    new URL("../app/components/Authentication/Frame.vue", import.meta.url),
    "utf8",
  );
  const form = await readFile(
    new URL(
      "../app/components/Authentication/PasswordForm.vue",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(page, /authentication.title/);
  assert.match(
    frame,
    /aria-hidden="true"[\s\S]*?class="[^"]*pointer-events-none/,
  );
  assert.match(frame, /radial-gradient/);
  assert.match(form, /message.type === ['"]error['"]/);
});

test("email routing uses Gateway GraphQL and preserves enterprise precedence", async () => {
  const connection = {
    domain: "acme.example",
    connection_id: "enterprise",
    tenant_id: "acme",
  };
  const directory = {
    async request(operation: string, variables: Record<string, unknown>) {
      assert.equal(operation, "loginProvider");
      assert.deepEqual(variables, {
        email: "alice@acme.example",
        localEnabled: true,
      });
      return JSON.stringify({ kind: "sso", connection });
    },
  };
  assert.deepEqual(
    await findLoginProvider(directory, "alice@acme.example", true),
    { kind: "sso", connection },
  );
});

test("local authentication requires Gateway approval and explicit deployment opt-in", async () => {
  const directory = {
    async request(operation: string, variables: Record<string, unknown>) {
      assert.equal(operation, "loginProvider");
      if (!variables.localEnabled)
        throw new Error("Registered provider required");
      return JSON.stringify({ kind: "local" });
    },
  };
  assert.deepEqual(
    await findLoginProvider(directory, "alice@demo.invalid", true),
    { kind: "local" },
  );
  await assert.rejects(
    findLoginProvider(directory, "alice@demo.invalid", false),
  );
});

test("email routing rejects malformed addresses before IO and never falls back after API errors", async () => {
  let requests = 0;
  const directory = {
    async request() {
      requests++;
      throw new Error("Unavailable");
    },
  };
  await assert.rejects(
    findLoginProvider(directory, "alice@@demo.invalid", true),
  );
  assert.equal(requests, 0);
  await assert.rejects(
    findLoginProvider(directory, "alice@demo.invalid", true),
  );
  assert.equal(requests, 1);
});

test("Kratos public sessions cannot supply administrator metadata", async () => {
  const original = globalThis.fetch;
  const now = Date.now();
  const identity = {
    id: "32d94b56-5499-4d85-b7b7-9ddaf70612a1",
    state: "active",
  };
  const requests: string[] = [];
  try {
    globalThis.fetch = async (url) => {
      assert.ok(url instanceof URL);
      requests.push(url.href);
      return new Response(
        JSON.stringify(
          url.port === "4434"
            ? {
                ...identity,
                metadata_admin: {
                  galadril: { tenant_id: "demo", user_id: "alice" },
                },
              }
            : {
                active: true,
                authenticated_at: new Date(now).toISOString(),
                expires_at: new Date(now + 60000).toISOString(),
                identity,
              },
        ),
      );
    };
    const result = await localIdentity(
      {
        kratos: "http://127.0.0.1:4433",
        kratosAdmin: "http://127.0.0.1:4434",
        directory: {
          async request(operation: string, variables: Record<string, unknown>) {
            assert.equal(operation, "bindLocal");
            assert.equal(variables.user, "alice");
            assert.equal(variables.external, identity.id);
            return true;
          },
        },
      },
      "__Host-galadril-identity=opaque",
      now,
    );
    assert.equal(result.subject, "alice");
    assert.deepEqual(requests, [
      "http://127.0.0.1:4433/sessions/whoami",
      `http://127.0.0.1:4434/admin/identities/${identity.id}`,
    ]);
  } finally {
    globalThis.fetch = original;
  }
});

test("local login trusts only fresh active Kratos sessions with administrator bindings", () => {
  const now = Date.now();
  const session = {
    active: true,
    authenticated_at: new Date(now).toISOString(),
    expires_at: new Date(now + 60000).toISOString(),
    identity: {
      id: "32d94b56-5499-4d85-b7b7-9ddaf70612a1",
      state: "active",
      metadata_admin: { galadril: { tenant_id: "demo", user_id: "alice" } },
      traits: { tenant_id: "attacker", user_id: "admin" },
    },
  };
  assert.deepEqual(localSessionIdentity(session, now, now), {
    tenant: "demo",
    subject: "alice",
    external: session.identity.id,
  });
  for (const patch of [
    { active: false },
    { authenticated_at: new Date(now - 60000).toISOString() },
    { authenticated_at: "invalid" },
    { expires_at: new Date(now - 1).toISOString() },
    { identity: { ...session.identity, state: "inactive" } },
    { identity: { ...session.identity, metadata_admin: {} } },
    { identity: { ...session.identity, id: "not-an-identity" } },
  ])
    assert.throws(() =>
      localSessionIdentity({ ...session, ...patch }, now, now),
    );
  assert.throws(() => localSessionIdentity(session, undefined, now));
});

test("private broker credentials must match connection, organization and product", async () => {
  const original = globalThis.fetch;
  const connection = { connection_id: "connection", tenant_id: "acme" };
  const record = {
    clientID: "connection",
    tenant: "acme",
    product: "galadril",
    clientSecret: "private-secret",
  };
  try {
    for (const change of [
      {},
      { tenant: "other" },
      { product: "other" },
      { clientID: "other" },
    ]) {
      globalThis.fetch = async (url, options) => {
        assert.ok(url instanceof URL);
        assert.equal(url.origin, "http://polis.internal");
        assert.equal(url.searchParams.get("clientID"), "connection");
        assert.equal(required(options).redirect, "error");
        assert.equal(
          new Headers(options?.headers).get("authorization"),
          "Api-Key private-api-key",
        );
        return new Response(JSON.stringify([{ ...record, ...change }]));
      };
      const result = connectionCredential(
        { polis: "http://polis.internal", polisApiKey: "private-api-key" },
        connection,
      );
      if (Object.keys(change).length) await assert.rejects(result);
      else assert.equal(await result, "private-secret");
    }
  } finally {
    globalThis.fetch = original;
  }
});

test("email discovery requests only canonical routing metadata through GraphQL", async () => {
  const directory = {
    async request(operation: string, variables: Record<string, unknown>) {
      assert.equal(operation, "loginProvider");
      assert.deepEqual(variables, {
        email: "alice@acme.example",
        localEnabled: false,
      });
      return JSON.stringify({
        kind: "sso",
        connection: {
          domain: "acme.example",
          tenant_id: "acme",
          connection_id: "connection",
        },
      });
    },
  };
  assert.equal(
    (await findConnection(directory, "alice@acme.example")).connection_id,
    "connection",
  );
});
test("email discovery selects only an exact registered domain", () => {
  const connections = [
    { domain: "acme.example", tenant_id: "acme", connection_id: "a" },
  ];
  assert.equal(
    discoverConnection("Alice@ACME.EXAMPLE", connections).tenant_id,
    "acme",
  );
  for (const email of [
    "a@evilacme.example",
    "a@acme.example.evil",
    "a@@acme.example",
    "a@acme.example\n",
    "a@unknown.example",
  ]) {
    assert.throws(() => discoverConnection(email, connections));
  }
  assert.throws(() =>
    discoverConnection("a@acme.example", [
      ...connections,
      { ...required(connections[0]), tenant_id: "other" },
    ]),
  );
});

test("cookie requests require the exact public origin", () => {
  requireOrigin("https://app.example", "https://app.example");
  for (const origin of [
    undefined,
    "null",
    "http://app.example",
    "https://app.example.evil",
  ]) {
    assert.throws(() => requireOrigin(origin, "https://app.example"));
  }
});

test("consent never grants unrequested audiences, clients, or permissions", () => {
  const request = {
    client: { client_id: "galadril-studio" },
    requested_scope: ["openid"],
    requested_access_token_audience: ["galadril"],
  };
  assert.deepEqual(validateConsent(request), {
    scope: ["openid"],
    audience: ["galadril"],
  });
  assert.throws(() =>
    validateConsent({ ...request, client: { client_id: "attacker" } }),
  );
  assert.throws(() =>
    validateConsent({ ...request, requested_scope: ["admin"] }),
  );
  assert.throws(() =>
    validateConsent({ ...request, requested_access_token_audience: ["other"] }),
  );
});

test("state comparison rejects absent, oversized and different values", () => {
  assert.equal(constantEqual("abc", "abc"), true);
  for (const state of [undefined, "", "abcd", "xyz", "x".repeat(10000)])
    assert.equal(constantEqual("abc", state), false);
});

test("upstream identity must belong to the selected connection", () => {
  const profile = { id: "external-user", requested: { client_id: "approved" } };
  assert.equal(externalSubject(profile, "approved"), "external-user");
  assert.throws(() => externalSubject(profile, "another"));
  assert.throws(() => externalSubject({ ...profile, id: "" }, "approved"));
});

test("Hydra redirects are bound to the configured public origin", () => {
  assert.equal(
    safeRedirect("https://app.example/oauth2/auth?x=y", "https://app.example"),
    "https://app.example/oauth2/auth?x=y",
  );
  for (const url of [
    "https://evil.example",
    "javascript:alert(1)",
    "//evil.example",
    "https://app.example@evil.example",
  ])
    assert.throws(() => safeRedirect(url, "https://app.example"));
});

test("the first-party server client supports rotating refresh tokens without service grants", () => {
  const client = clientDefinition({
    origin: "https://app.example",
    clientSecret: "secret",
  });
  assert.deepEqual(client.grant_types, ["authorization_code", "refresh_token"]);
  assert.equal(client.scope, "openid offline_access");
  assert.deepEqual(client.redirect_uris, ["https://app.example/auth/callback"]);
  assert.deepEqual(client.audience, ["galadril"]);
  assert.equal(client.access_token_strategy, "jwt");
  assert.equal(client.skip_consent, false);
});

test("consent grants refresh tokens only to the first-party requested scopes", () => {
  const request = {
    client: { client_id: "galadril-studio" },
    requested_scope: ["openid", "offline_access"],
    requested_access_token_audience: ["galadril"],
  };
  assert.deepEqual(validateConsent(request).scope, [
    "openid",
    "offline_access",
  ]);
  assert.throws(() =>
    validateConsent({
      ...request,
      requested_scope: ["openid", "offline_access", "admin"],
    }),
  );
});

test("access claims bind one local identity and reject ID tokens", () => {
  const claims = {
    sub: "user",
    tenant_id: "acme",
    iss: "https://issuer.example",
    aud: "galadril",
    iat: 100,
    nbf: 100,
    exp: 200,
    token_use: "access",
  };
  assert.equal(accessClaims(claims).tenant_id, "acme");
  for (const patch of [
    { tenant_id: "../other" },
    { tenant_id: "a".repeat(65) },
    { tenant_id: "" },
    { sub: "" },
    { token_use: "id" },
    { exp: undefined },
    { iat: 300 },
  ])
    assert.throws(() => accessClaims({ ...claims, ...patch }));
});

test("JWT verification rejects forged signatures and wrong issuer or audience", async () => {
  const keys = await generateKeyPair("RS256");
  const wrongKeys = await generateKeyPair("RS256");
  const sign = (key: CryptoKey, overrides = {}) =>
    new SignJWT({ tenant_id: "acme", token_use: "access", ...overrides })
      .setProtectedHeader({ alg: "RS256", kid: "test" })
      .setSubject("user")
      .setIssuer("https://issuer.example")
      .setAudience("galadril")
      .setIssuedAt()
      .setNotBefore("0s")
      .setExpirationTime("5m")
      .sign(key);
  const jwk = await exportJWK(keys.publicKey);
  const resolve = async () => keys.publicKey;
  assert.equal(
    (
      await verifyAccessToken(
        await sign(keys.privateKey),
        "https://issuer.example",
        resolve,
      )
    ).sub,
    "user",
  );
  await assert.rejects(
    verifyAccessToken(
      await sign(wrongKeys.privateKey),
      "https://issuer.example",
      resolve,
    ),
  );
  await assert.rejects(
    verifyAccessToken(
      await sign(keys.privateKey),
      "https://other.example",
      resolve,
    ),
  );
  const otherAudience = await new SignJWT({
    tenant_id: "acme",
    token_use: "access",
  })
    .setProtectedHeader({ alg: "RS256" })
    .setSubject("user")
    .setIssuer("https://issuer.example")
    .setAudience("another-api")
    .setIssuedAt()
    .setExpirationTime("5m")
    .sign(keys.privateKey);
  await assert.rejects(
    verifyAccessToken(otherAudience, "https://issuer.example", resolve),
  );
  assert.equal(jwk.kty, "RSA");
});
