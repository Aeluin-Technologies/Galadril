import assert from "node:assert/strict";
import test from "node:test";
import {
  permissionManifest,
  permissionBlocks,
  normalizeCompanyDomain,
  normalizeProviderAddress,
  normalizeDiscoveryAddress,
  providerFormValues,
} from "../app/utils/administration.mjs";

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
