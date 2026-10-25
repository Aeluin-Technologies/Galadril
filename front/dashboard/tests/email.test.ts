import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseAccounts,
  connectionUpdate,
  can,
  parseDiscovery,
  validMailHost,
  renderEmailSignature,
  defaultEmailSignature,
  parseDrafts,
} from "../app/utils/email.ts";

test("draft editing preserves Bcc recipients", () => {
  const draft = parseDrafts([
    {
      id: "d",
      revision: 1,
      status: "draft",
      delivery_id: "<d@example.org>",
      automatic: false,
      content: {
        to: ["to@example.org"],
        cc: [],
        bcc: ["private@example.org"],
        subject: "Hello",
        body: "Body",
        reply_to_message_id: "",
        references: "",
        attachments: [],
      },
    },
  ])[0];
  assert.deepEqual(draft?.content.bcc, ["private@example.org"]);
});

test("signature previews escape automatic fields and retain the default reference", () => {
  assert.equal(defaultEmailSignature, "Ref. : <code>{{ ref }}</code>");
  assert.equal(
    renderEmailSignature(
      defaultEmailSignature + " {{ subject }} {{ __proto__ }}",
      { ref: "GDR-example", subject: '<img src="https://tracker.example/x">' },
    ),
    "Ref. : <code>GDR-example</code> &lt;img src=&quot;https://tracker.example/x&quot;&gt; {{ __proto__ }}",
  );
});

test("DNS suggestions accept only supported encrypted mail settings", () => {
  const result = {
    domain: "example.org",
    imap: [
      {
        host: "imap.provider.org",
        port: 993,
        security: "tls",
        priority: 0,
        weight: 0,
      },
    ],
    smtp: [
      {
        host: "smtp.provider.org",
        port: 587,
        security: "starttls",
        priority: 0,
        weight: 0,
      },
    ],
    imap_status: "found",
    smtp_status: "found",
  };
  assert.equal(parseDiscovery(result).smtp[0]?.security, "starttls");
  assert.throws(() =>
    parseDiscovery({ ...result, smtp: [{ ...result.smtp[0], port: 25 }] }),
  );
  assert.throws(() =>
    parseDiscovery({
      ...result,
      smtp: [{ ...result.smtp[0], port: 465, security: "starttls" }],
    }),
  );
  assert.throws(() => parseDiscovery({ ...result, imap_status: "invented" }));
  assert.throws(() => parseDiscovery({ ...result, password: "secret" }));
});

test("connection metadata rejects secrets and permissions never inherit from pipelines", () => {
  const account = {
    id: "a".repeat(32),
    revision: 1,
    rights: ["view", "edit", "delete"],
    config: {
      name: "Work",
      address: "me@example.org",
      imap_host: "imap.example.org",
      smtp_host: "smtp.example.org",
      imap_port: 993,
      smtp_port: 465,
      folders: ["INBOX"],
      enabled: true,
      ingest: false,
      poll_seconds: 120,
    },
    sync_status: "pending",
    last_sync: 0,
    has_credentials: true,
  };
  const [parsed] = parseAccounts(JSON.stringify([account]));
  assert.ok(parsed);
  assert.equal(can(parsed, "edit"), true);
  assert.equal(can(parsed, "reveal"), false);
  assert.equal(parsed.config.initial_sync, "new");
  assert.equal(parsed.can_manage_access, false);
  assert.equal("credentials" in connectionUpdate(parsed, "", ""), false);
  assert.throws(() =>
    parseAccounts([{ ...account, credentials: { password: "secret" } }]),
  );
  assert.throws(() => parseAccounts([{ ...account, rights: ["invented"] }]));
});

test("password rotation can preserve the server-side login", () => {
  const [account] = parseAccounts([
    {
      id: "b".repeat(32),
      revision: 2,
      rights: ["edit"],
      config: {
        name: "Work",
        address: "me@example.org",
        imap_host: "imap.example.org",
        smtp_host: "smtp.example.org",
        imap_port: 993,
        smtp_port: 465,
        folders: ["INBOX"],
        enabled: true,
        ingest: false,
        poll_seconds: 120,
      },
      sync_status: "ok",
      last_sync: 1,
      has_credentials: true,
    },
  ]);
  assert.ok(account);
  assert.throws(() => connectionUpdate(account, "login", ""));
  assert.deepEqual(connectionUpdate(account, "", "replacement").credentials, {
    password: "replacement",
  });
  assert.deepEqual(
    connectionUpdate(account, "login", "new-password").credentials,
    { username: "login", password: "new-password" },
  );
});

test("manual mail settings require hostnames rather than URLs or arbitrary text", () => {
  for (const host of [
    "imap.etu.univ-lorraine.fr",
    "smtp.gmail.com",
    "bücher.example",
  ])
    assert.equal(validMailHost(host), true);
  for (const host of [
    "anything",
    "a..org",
    "https://imap.example.org",
    "foo/bar",
    "host:993",
    "127.0.0.1",
    "foo bar.org",
    "a.-bad.org",
  ])
    assert.equal(validMailHost(host), false);
});
