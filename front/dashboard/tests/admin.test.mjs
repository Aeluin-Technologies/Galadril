import assert from "node:assert/strict";
import test from "node:test";
import {
  permissionManifest,
  permissionBlocks,
  normalizeCompanyDomain,
  normalizeProviderAddress,
  normalizeDiscoveryAddress,
  providerFormValues,
  administrationResult,
  administrationError,
  accessGraphLayout,
  generalAccessDraft,
  resourceAccessOptions,
} from "../app/utils/administration.mjs";

test("administration responses unwrap the namespace and preserve safe actionable error codes", () => {
  assert.deepEqual(
    administrationResult({ data: { administration: { users: [] } } }),
    { users: [] },
  );
  assert.throws(() => administrationResult({ data: { users: [] } }));
  assert.throws(
    () =>
      administrationResult({
        errors: [
          {
            message: "database password=secret",
            extensions: { code: "LAST_ADMINISTRATOR" },
          },
        ],
      }),
    /last active administrator/i,
  );
  assert.equal(
    administrationError(
      new Error("private database details"),
      "Your team could not be loaded. Use Reload people to try again.",
    ),
    "Your team could not be loaded. Use Reload people to try again.",
  );
});

test("automatic group imports start with the real Polis OIDC claim envelope", async () => {
  const { roleSyncDefaults } = await import("../app/utils/administration.mjs");
  assert.deepEqual(roleSyncDefaults("company-connection"), {
    connectionId: "company-connection",
    claim: "raw.groups",
    enabled: false,
    mappings: [],
  });
});

test("resource access controls require a visible resource and a supported permission", async () => {
  const { canSetResourceAccess } =
    await import("../app/utils/administration.mjs");
  assert.equal(canSetResourceAccess(undefined, "reader"), false);
  assert.equal(canSetResourceAccess("document", ""), false);
  assert.equal(canSetResourceAccess("document", "executor"), false);
  assert.equal(canSetResourceAccess("unknown", "reader"), false);
  assert.equal(canSetResourceAccess("document", "reader"), true);
  assert.equal(canSetResourceAccess("pipeline", "executor"), true);
});

test("named access rules have stable bounded identifiers without silently truncating names", async () => {
  const { accessRuleIdentifier } =
    await import("../app/utils/administration.mjs");
  assert.equal(accessRuleIdentifier("  Office hours  "), "office-hours");
  assert.equal(accessRuleIdentifier("Équipe – horaires"), "equipe-horaires");
  assert.equal(accessRuleIdentifier("Support_evening"), "support_evening");
  assert.notEqual(
    accessRuleIdentifier("Office hours"),
    accessRuleIdentifier("Evening support"),
  );
  for (const name of ["", "---", "a".repeat(65), "权限"])
    assert.throws(() => accessRuleIdentifier(name), /rule name/i);
});

test("including another role grants its access to the selected role, never the reverse", async () => {
  const { roleInclusion } = await import("../app/utils/administration.mjs");
  assert.deepEqual(roleInclusion("senior", "junior", true), {
    role: "junior",
    parent: "senior",
    enabled: true,
  });
  assert.throws(() => roleInclusion("senior", "senior", true));
});

test("the access map uses server-filtered nodes and never reveals dangling relationships", () => {
  const graph = accessGraphLayout({
    nodes: [
      { id: "user:alice", kind: "user", label: "Alice" },
      { id: "role:reader", kind: "role", label: "Reader" },
      { id: "document:report", kind: "document", label: "Quarterly report" },
    ],
    edges: [
      { source: "user:alice", target: "role:reader", relation: "member" },
      { source: "role:reader", target: "document:report", relation: "reader" },
      { source: "user:alice", target: "document:secret", relation: "reader" },
    ],
    truncated: true,
  });
  assert.equal(graph.nodes.length, 3);
  assert.equal(graph.edges.length, 2);
  assert.equal(graph.truncated, true);
  assert.equal(graph.nodes[0].position.x, 0);
  assert.ok(graph.nodes[1].position.x < graph.nodes[2].position.x);
  assert.throws(() =>
    accessGraphLayout({
      nodes: [
        { id: "duplicate", kind: "user", label: "One" },
        { id: "duplicate", kind: "role", label: "Two" },
      ],
      edges: [],
    }),
  );
});

test("guided general rules validate UTC windows and country codes before saving", () => {
  assert.deepEqual(
    generalAccessDraft({
      roles: ["reader"],
      start: "08:00",
      end: "18:00",
      regions: " fr, de ",
    }),
    {
      roleNames: ["reader"],
      startHourUtc: 8,
      endHourUtc: 18,
      regions: ["FR", "DE"],
    },
  );
  assert.equal(
    generalAccessDraft({ roles: [], start: "22:00", end: "06:00", regions: "" })
      .endHourUtc,
    6,
  );
  assert.equal(
    generalAccessDraft({ roles: [], start: "22:00", end: "00:00", regions: "" })
      .endHourUtc,
    24,
  );
  assert.throws(
    () =>
      generalAccessDraft({
        roles: [],
        start: "08:00",
        end: "08:00",
        regions: "",
      }),
    /different/i,
  );
  assert.throws(
    () =>
      generalAccessDraft({
        roles: [],
        start: "08:30",
        end: "18:00",
        regions: "",
      }),
    /whole hours/i,
  );
  assert.throws(
    () =>
      generalAccessDraft({ roles: [], start: "", end: "", regions: "Paris" }),
    /country/i,
  );
  assert.throws(
    () => generalAccessDraft({ roles: [], start: "", end: "", regions: "" }),
    /time.*region/i,
  );
});

test("access maps support every Gateway resource kind and offer only compatible grants", () => {
  const kinds = [
    "group",
    "project",
    "table",
    "raw",
    "document",
    "ontology",
    "pipeline",
    "conversation",
    "entity_state",
    "event",
  ];
  assert.equal(
    accessGraphLayout({
      nodes: kinds.map((kind) => ({
        id: `${kind}:example`,
        kind,
        label: kind,
      })),
      edges: [],
    }).nodes.length,
    kinds.length,
  );
  assert.deepEqual(
    resourceAccessOptions("raw").map((item) => item.value),
    ["reader"],
  );
  assert.deepEqual(
    resourceAccessOptions("group").map((item) => item.value),
    ["reader", "ingester"],
  );
  assert.deepEqual(
    resourceAccessOptions("pipeline").map((item) => item.value),
    ["reader", "editor", "executor", "administrator"],
  );
  assert.deepEqual(resourceAccessOptions("tenant"), []);
});

test("detection accepts metadata URLs without confusing SAML queries with OIDC issuers", () => {
  assert.equal(
    normalizeDiscoveryAddress("accounts.google.com"),
    "https://accounts.google.com",
  );
  assert.equal(
    normalizeDiscoveryAddress(" login.acme.com/metadata.xml?appid=123 "),
    "https://login.acme.com/metadata.xml?appid=123",
  );
  assert.equal(
    normalizeDiscoveryAddress(
      "https://login.acme.com/.well-known/openid-configuration",
    ),
    "https://login.acme.com/.well-known/openid-configuration",
  );
  for (const value of [
    "http://idp.example",
    "https://user:secret@idp.example",
    "https://idp.example/#fragment",
  ])
    assert.throws(() => normalizeDiscoveryAddress(value));
});

test("detected settings prefill only the selected protocol and never app credentials", () => {
  assert.deepEqual(
    providerFormValues({
      protocol: "oidc",
      issuer: "https://login.acme.com/Realm",
      client_secret: "never-prefill",
    }),
    { protocol: "oidc", issuer: "https://login.acme.com/Realm", metadata: "" },
  );
  assert.deepEqual(
    providerFormValues({ protocol: "saml", saml_metadata: "<metadata/>" }),
    { protocol: "saml", issuer: "", metadata: "<metadata/>" },
  );
  for (const value of [
    { protocol: "oauth" },
    { protocol: "oidc", issuer: "http://idp.example" },
    { protocol: "saml", saml_metadata: "" },
  ])
    assert.throws(() => providerFormValues(value));
});

test("company domains accept work emails and website addresses without changing ownership", () => {
  for (const value of [
    " Acme.COM ",
    " Alice@Acme.COM ",
    "https://acme.com/about",
    "acme.com.",
  ]) {
    assert.equal(normalizeCompanyDomain(value), "acme.com");
  }
  assert.equal(normalizeCompanyDomain("person@bücher.de"), "xn--bcher-kva.de");
  for (const value of [
    "alice@",
    "a@b@acme.com",
    "acme.com/path",
    "acme.com:443",
    "https://alice:secret@acme.com",
    "a@acme.com/path",
    "*.acme.com",
    "localhost",
    "127.0.0.1",
  ]) {
    assert.throws(() => normalizeCompanyDomain(value), value);
  }
});

test("provider addresses accept missing HTTPS and discovery URLs while preserving issuer paths", () => {
  assert.equal(
    normalizeProviderAddress("login.acme.com:8443/Realm"),
    "https://login.acme.com:8443/Realm",
  );
  assert.equal(
    normalizeProviderAddress(" login.Acme.com/Realm/ "),
    "https://login.acme.com/Realm/",
  );
  assert.equal(
    normalizeProviderAddress(
      "https://login.acme.com/Realm/.well-known/openid-configuration",
    ),
    "https://login.acme.com/Realm",
  );
  assert.equal(
    normalizeProviderAddress("https://login.acme.com"),
    "https://login.acme.com",
  );
  for (const value of [
    "http://login.acme.com",
    "https://alice:secret@login.acme.com",
    "https://login.acme.com?tenant=other",
    "https://login.acme.com/#fragment",
    "alice@acme.com",
    "",
  ]) {
    assert.throws(() => normalizeProviderAddress(value), value);
  }
});

test("visual permission blocks round-trip without changing their scope", () => {
  const blocks = [
    { kind: "role_assignment", user: "alice", role: "analyst", enabled: true },
    {
      kind: "relationship",
      resource: { type: "group", id: "sales" },
      relation: "reader",
      subject: { type: "user", id: "alice" },
      enabled: false,
    },
    {
      kind: "cedar_policy",
      id: "policy_1",
      content: "permit(principal, action, resource);",
      active: true,
    },
  ];
  assert.deepEqual(
    permissionBlocks(JSON.stringify(permissionManifest(blocks))),
    blocks,
  );
});

test("visual imports reject unsupported shapes instead of silently discarding code", () => {
  assert.throws(() => permissionBlocks('{"version":1,"operations":[]}'));
  assert.throws(() =>
    permissionBlocks(
      '{"version":1,"tenant":"other","operations":[{"kind":"role_assignment","user":"alice","role":"admin","enabled":true}]}',
    ),
  );
  assert.throws(() =>
    permissionManifest([
      {
        kind: "relationship",
        resource: { type: "tenant", id: "other" },
        relation: "administrator",
        subject: { type: "user", id: "alice" },
        enabled: true,
      },
    ]),
  );
  assert.throws(() =>
    permissionManifest([
      {
        kind: "role_assignment",
        user: "../alice",
        role: "admin",
        enabled: true,
      },
    ]),
  );
  assert.throws(() =>
    permissionManifest(
      Array.from({ length: 65 }, () => ({
        kind: "role_assignment",
        user: "alice",
        role: "analyst",
        enabled: true,
      })),
    ),
  );
});

test("administrators can reach people directly from the command bar", async () => {
  const { administrationActions } = await import("../app/utils/account.mjs");
  assert.ok(
    administrationActions(true).some(
      (action) => action.href === "/settings/sso#users",
    ),
  );
});

test("in-place passkey recovery preserves only the safe reauthentication classification", async () => {
  const { beginStepUp } = await import("../app/utils/auth-navigation.mjs");
  await assert.rejects(
    beginStepUp("https://app.example", async () => ({
      ok: false,
      status: 401,
      json: async () => ({
        data: { code: "REAUTHENTICATION_REQUIRED" },
        message: "private failure details",
      }),
    })),
    (error) =>
      error.code === "REAUTHENTICATION_REQUIRED" &&
      !error.message.includes("private"),
  );
  await assert.rejects(
    beginStepUp("https://app.example", async () => ({
      ok: false,
      status: 503,
      json: async () => ({
        data: { code: "UNTRUSTED_UPSTREAM_DETAIL" },
        message: "secret",
      }),
    })),
    (error) =>
      error.code !== "UNTRUSTED_UPSTREAM_DETAIL" &&
      !error.message.includes("secret"),
  );
});

test("an expired passkey step-up returns to primary sign-in instead of retrying forever", async () => {
  const { beginStepUp } = await import("../app/utils/auth-navigation.mjs");
  const { sessionFailureAction } =
    await import("../app/utils/session-activity.mjs");
  await assert.rejects(
    beginStepUp("https://app.example", async () => ({
      ok: false,
      status: 401,
      json: async () => ({ data: { code: "SESSION_UNAVAILABLE" } }),
    })),
    (error) =>
      error.code === "SESSION_UNAVAILABLE" &&
      sessionFailureAction(error) === "reauthenticate",
  );
});

test("session heartbeats only extend visible active sessions and keep upstream outages recoverable", async () => {
  const { shouldRefreshSession, sessionFailureAction } =
    await import("../app/utils/session-activity.mjs");
  const active = {
    hidden: false,
    modalOpen: false,
    path: "/settings/sso",
    lastActivity: 1000,
    now: 61000,
  };
  assert.equal(shouldRefreshSession(active), true);
  assert.equal(shouldRefreshSession({ ...active, hidden: true }), false);
  assert.equal(shouldRefreshSession({ ...active, modalOpen: true }), false);
  assert.equal(shouldRefreshSession({ ...active, path: "/login" }), false);
  assert.equal(shouldRefreshSession({ ...active, now: 301001 }), false);
  assert.equal(sessionFailureAction({ statusCode: 401 }), "reauthenticate");
  assert.equal(sessionFailureAction({ statusCode: 428 }), "passkey");
  assert.equal(sessionFailureAction({ statusCode: 503 }), "retry");
  assert.equal(
    sessionFailureAction(new TypeError("network unavailable")),
    "retry",
  );
});
