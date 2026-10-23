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

test("command search understands French and English navigation and action intents", async () => {
  const { searchNavigation } =
    await import("../app/utils/navigation-search.mjs");
  const context = { administrator: true, locale: "fr" };
  for (const [query, expected] of [
    ["administration", "administration"],
    ["utilisateurs", "people"],
    ["gestion des utilisateurs", "people"],
    ["utilisateurs gestion", "people"],
    ["créer un rôle", "create-role"],
    ["cre\u0301er un ro\u0302le", "create-role"],
    ["CREER ROLE", "create-role"],
    ["create a role", "create-role"],
    ["role", "permissions"],
    ["rôles", "permissions"],
    ["gestion des rôles", "permissions"],
    ["gérer les membres", "people"],
    ["administrer les utilisateurs", "people"],
    ["ajouter un rôle", "create-role"],
    ["création de rôle", "create-role"],
    ["single sign on", "sign-in"],
    ["documents", "documents"],
    ["nouvelle pipeline", "create-pipeline"],
  ]) {
    const matches = searchNavigation(query, context);
    assert.equal(matches[0]?.id, expected, query);
    assert.ok(matches.every((item) => item.href.startsWith("/")));
  }
  const role = searchNavigation("créer un rôle", context)[0];
  assert.equal(role.kind, "action");
  assert.equal(role.label, "Créer un rôle");
  assert.equal(role.href, "/settings/sso?task=create-role#permissions");
});

test("command search never offers administrative actions without live authorization", async () => {
  const { searchNavigation } =
    await import("../app/utils/navigation-search.mjs");
  for (const administrator of [false, undefined, null, "true"]) {
    for (const query of ["administration", "utilisateurs", "create role"])
      assert.deepEqual(searchNavigation(query, { administrator }), []);
    assert.equal(
      searchNavigation("documents", { administrator })[0]?.id,
      "documents",
    );
  }
});

test("command search has bounded literal matching and cannot create arbitrary destinations", async () => {
  const { searchNavigation } =
    await import("../app/utils/navigation-search.mjs");
  for (const query of [
    "",
    "de la et",
    "a".repeat(513),
    "javascript:alert(1)",
    "https://attacker.invalid",
  ])
    assert.deepEqual(searchNavigation(query, { administrator: true }), []);
  assert.equal(
    searchNavigation("user manag", { administrator: true })[0]?.id,
    "people",
  );
});

test("command shortcuts list implemented destinations and preserve administrative visibility", async () => {
  const { navigationEntries } =
    await import("../app/utils/navigation-search.mjs");
  const publicEntries = navigationEntries({
    administrator: false,
    locale: "fr",
  });
  assert.ok(publicEntries.some((item) => item.id === "documents"));
  assert.ok(!publicEntries.some((item) => item.href.startsWith("/settings")));
  assert.ok(
    navigationEntries({ administrator: true }).some(
      (item) => item.id === "create-role",
    ),
  );
  assert.ok(
    publicEntries.every((item) => item.href.startsWith("/") && !item.execute),
  );
});

test("navigation merging deduplicates stable IDs and rejects external destinations", async () => {
  const { mergeNavigation } =
    await import("../app/utils/navigation-search.mjs");
  const local = [
    {
      id: "people",
      kind: "navigation",
      href: "/settings/sso#users",
      label: "People",
      description: "Manage members",
    },
  ];
  const remote = [
    { ...local[0], label: "Translated people" },
    {
      id: "law-research",
      kind: "action",
      href: "/plugins/law?task=research",
      label: "Legal research",
      description: "Open research",
    },
    {
      id: "invalid",
      kind: "navigation",
      href: "//attacker.invalid",
      label: "Bad",
      description: "Bad",
    },
  ];
  const merged = mergeNavigation(local, remote);
  assert.deepEqual(
    merged.map((entry) => entry.id),
    ["people", "law-research"],
  );
  assert.equal(merged[0].label, "People");
  assert.equal(
    mergeNavigation(
      [],
      Array.from({ length: 20 }, (_, index) => ({
        ...local[0],
        id: `page-${index}`,
      })),
    ).length,
    12,
  );
});

test("command history is bounded and belongs to the current account", async () => {
  const { readCommandHistory, writeCommandHistory } =
    await import("../app/utils/command-history.mjs");
  const scope = { tenant: "demo", subject: "alice" };
  const encoded = writeCommandHistory(
    ["role", "role", "library", "chat", "users", "settings", "pipeline"],
    scope,
  );
  assert.deepEqual(readCommandHistory(encoded, scope), [
    "role",
    "library",
    "chat",
    "users",
    "settings",
  ]);
  assert.deepEqual(
    readCommandHistory(encoded, { tenant: "demo", subject: "bob" }),
    [],
  );
  assert.deepEqual(
    readCommandHistory(encoded, { tenant: "other", subject: "alice" }),
    [],
  );
  assert.deepEqual(readCommandHistory("x".repeat(8193), scope), []);
  assert.deepEqual(readCommandHistory("{broken", scope), []);
  assert.deepEqual(
    readCommandHistory(JSON.stringify(["legacy-unscoped"]), scope),
    [],
  );
  assert.deepEqual(
    readCommandHistory(
      writeCommandHistory(["x".repeat(513), "bad\nquery", " role "], scope),
      scope,
    ),
    ["role"],
  );
  assert.deepEqual(readCommandHistory(encoded, null), []);
});

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

test("schema incompatibility gives an actionable message without leaking backend details", () => {
  const message = "The administration service needs to be updated.";
  const errors = [
    {
      message: 'Unknown field "displayName" on type "Role"',
      locations: [{ line: 1, column: 307 }],
    },
  ];
  assert.equal(
    administrationError({ data: { errors } }, "Try again.", message),
    message,
  );
  let failure;
  try {
    administrationResult({ errors });
  } catch (error) {
    failure = error;
  }
  assert.equal(administrationError(failure, "Try again.", message), message);
  assert.equal(
    administrationError(
      { data: { errors: [{ message: "private database failure" }] } },
      "Try again.",
      message,
    ),
    "Try again.",
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

test("parallel permissions share one selectable connection without overlapping labels", () => {
  const graph = accessGraphLayout({
    nodes: [
      {
        id: "role:reader",
        kind: "role",
        label: "Reader",
        resourceId: "reader",
      },
      {
        id: "document:report",
        kind: "document",
        label: "Report",
        resourceId: "report",
      },
    ],
    edges: [
      { source: "role:reader", target: "document:report", relation: "owner" },
      { source: "role:reader", target: "document:report", relation: "reader" },
      { source: "role:reader", target: "document:report", relation: "owner" },
    ],
  });
  assert.equal(graph.edges.length, 1);
  assert.deepEqual(graph.edges[0].data.relations, ["owner", "reader"]);
  assert.equal(graph.edges[0].label, "2 permissions");
  assert.ok(graph.nodes[1].position.x - graph.nodes[0].position.x >= 320);
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

test("active sessions refresh near expiry or after five minutes, never every minute", async () => {
  const { shouldRefreshSession } =
    await import("../app/utils/session-activity.mjs");
  const active = {
    hidden: false,
    modalOpen: false,
    path: "/pengolo",
    lastActivity: 600_000,
    now: 600_000,
    expiresAt: 1500,
    lastCheck: 540_000,
  };
  assert.equal(shouldRefreshSession(active), false);
  assert.equal(shouldRefreshSession({ ...active, lastCheck: 300_000 }), true);
  assert.equal(shouldRefreshSession({ ...active, expiresAt: 700 }), true);
  assert.equal(
    shouldRefreshSession({ ...active, expiresAt: 700, lastCheck: 590_000 }),
    false,
  );
  assert.equal(
    shouldRefreshSession({ ...active, lastCheck: 0, hidden: true }),
    false,
  );
});

test("permission imports use typed GraphQL choices without serialized JSON arguments", async () => {
  const { permissionImportInput } =
    await import("../app/utils/administration.mjs");
  assert.equal(typeof permissionImportInput, "function");
  assert.deepEqual(
    permissionImportInput({
      version: 1,
      operations: [
        {
          kind: "role_assignment",
          user: "alice",
          role: "reader",
          enabled: true,
        },
        {
          kind: "relationship",
          resource: { type: "group", id: "reports" },
          relation: "reader",
          subject: { type: "role", id: "reader" },
          enabled: true,
        },
      ],
    }),
    {
      version: 1,
      operations: [
        { roleAssignment: { user: "alice", role: "reader", enabled: true } },
        {
          relationship: {
            resource: { kind: "group", id: "reports" },
            relation: "reader",
            subject: { kind: "role", id: "reader" },
            enabled: true,
          },
        },
      ],
    },
  );
  assert.throws(() =>
    permissionImportInput({ version: 1, operations: [{ kind: "unknown" }] }),
  );
  assert.throws(() =>
    permissionImportInput({
      version: 1,
      operations: [
        {
          kind: "role_assignment",
          user: "alice",
          role: "reader",
          enabled: true,
          tenant: "other",
        },
      ],
    }),
  );
});

test("paged search ignores stale replies and clears prior results after access failure", async () => {
  const { createPagedSearch } = await import("../app/utils/paged-search.mjs");
  let release;
  let state;
  const calls = [];
  const pager = createPagedSearch(
    async (query, after) => {
      calls.push([query, after]);
      if (query === "old")
        return new Promise((resolve) => {
          release = resolve;
        });
      if (after) throw new Error("Access revoked");
      return { items: [{ id: query }], after: "next" };
    },
    (value) => {
      state = value;
    },
  );
  const stale = pager.search("old");
  await pager.search("élodie");
  release({ items: [{ id: "private" }], after: "old-cursor" });
  await stale;
  assert.deepEqual(state.items, [{ id: "élodie" }]);
  assert.equal(state.after, "next");
  await Promise.all([pager.more(), pager.more()]);
  assert.equal(calls.filter(([, after]) => after).length, 1);
  assert.deepEqual(state.items, []);
  assert.equal(state.after, null);
  assert.equal(state.failed, true);
  pager.clear();
  assert.equal(state.failed, false);
});

test("paged search cancels superseded requests and bounds retained results", async () => {
  const { createPagedSearch } = await import("../app/utils/paged-search.mjs");
  const signals = [];
  let state;
  let release;
  let page = 0;
  const pager = createPagedSearch(
    async (query, after, signal) => {
      signals.push(signal);
      if (query === "old")
        return new Promise((resolve) => {
          release = resolve;
        });
      return {
        items: Array.from({ length: 20 }, (_, index) => ({
          id: page * 20 + index,
        })),
        after: String(++page),
      };
    },
    (value) => {
      state = value;
    },
  );
  const stale = pager.search("old");
  pager.prepare("next");
  assert.equal(signals[0].aborted, true);
  assert.equal(state.loading, true);
  assert.deepEqual(state.items, []);
  await pager.search("next");
  for (let index = 0; index < 10; index++) await pager.more();
  assert.equal(state.items.length, 100);
  assert.equal(state.after, null);
  assert.equal(signals.length, 6);
  release({ items: [{ id: "private" }], after: "old" });
  await stale;
  assert.equal(state.items.length, 100);
  pager.prepare("unfinished");
  pager.clear();
  assert.equal(state.loading, false);
  const before = signals.length;
  await pager.search("x".repeat(513));
  assert.equal(signals.length, before);
  assert.equal(state.failed, true);
});
