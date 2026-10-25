import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseEmailThread,
  relatedEmailExecutions,
} from "../app/utils/email-thread.ts";

test("thread traces match native provenance and never arbitrary body text", () => {
  const message = {
    root: "<root@example.org>",
    message_id: "<root@example.org>",
    parent: "",
    subject: "Help",
    date: "",
    source_key: "tenant/raw/email/source.json",
    model: "local",
    analysis: "Reviewed",
    number: 1,
    reference: "<root@example.org>/1",
    received_id: "a".repeat(32),
    draft_id: null,
    ingested: true,
    status: "received",
  };
  const thread = parseEmailThread({
    thread_id: message.root,
    messages: [message],
    truncated: false,
    indexed_complete: true,
  });
  const trace = (id: string, correlationId: string, result: unknown) => ({
    commandId: id,
    pipelineId: "pipeline",
    step: "route",
    status: "completed",
    correlationId,
    result,
  });
  const rows = [
    trace("first", "c", {
      output: {
        record: {
          raw_payload: {
            metadata: { account_id: "account", email_id: message.received_id },
          },
        },
      },
    }),
    trace("next", "c", null),
    trace("foreign", "other", {
      output: {
        record: {
          raw_payload: {
            metadata: { account_id: "other", email_id: message.received_id },
          },
        },
      },
    }),
    trace("body", "body", {
      body: {
        metadata: { account_id: "account", email_id: message.received_id },
      },
    }),
  ];
  const projected = rows.map((row) =>
    row.commandId === "first"
      ? {
          ...row,
          sourceEmailAccountId: "account",
          sourceEmailId: message.received_id,
        }
      : row,
  );
  assert.deepEqual(
    relatedEmailExecutions(projected, "account", thread.messages).map(
      (row) => row.commandId,
    ),
    ["first", "next"],
  );
  assert.throws(() =>
    parseEmailThread({ ...thread, messages: Array(129).fill(message) }),
  );
});
import {
  parseAccounts,
  connectionUpdate,
  can,
  parseDiscovery,
  validMailHost,
  renderEmailSignature,
  defaultEmailSignature,
  parseDrafts,
  emailAccountActions,
} from "../app/utils/email.ts";

test("account actions stay grouped and retain independent credential permissions", () => {
  assert.deepEqual(emailAccountActions(["view"]), []);
  assert.deepEqual(emailAccountActions(["view", "edit"]), ["edit"]);
  assert.deepEqual(
    emailAccountActions(["use", "edit", "share", "reveal", "delete"]),
    ["edit", "reveal"],
  );
  assert.deepEqual(emailAccountActions(["use", "edit", "reveal"], "servers"), [
    "test",
    "servers",
  ]);
  assert.deepEqual(
    emailAccountActions(["edit", "share", "delete"], "management"),
    ["access", "delete"],
  );
});

test("draft editing preserves Bcc recipients and the human tracking choice", () => {
  const draft = parseDrafts([
    {
      id: "d",
      revision: 1,
      status: "draft",
      delivery_id: "<d@example.org>",
      automatic: false,
      content: {
        track_opens: true,
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
  assert.equal(draft?.content.track_opens, true);
});

test("signatures start empty and explicit reference fields are safely escaped", () => {
  assert.equal(defaultEmailSignature, "");
  assert.equal(
    renderEmailSignature(
      "Ref. : <code>{{ ref }}</code> {{ subject }} {{ __proto__ }}",
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

test("sending-only accounts retain supported SMTP2GO ports without an IMAP server", () => {
  for (const port of [25, 80, 443, 465, 587, 2525, 8025, 8465]) {
    const [account] = parseAccounts([
      {
        id: "a".repeat(32),
        revision: 1,
        rights: ["view", "send"],
        has_credentials: true,
        sync_status: "pending",
        last_sync: 0,
        config: {
          name: "Relay",
          address: "me@example.org",
          smtp_host: "mail-eu.smtp2go.com",
          smtp_port: port,
          imap_port: 993,
          folders: ["INBOX"],
          enabled: true,
          ingest: false,
          poll_seconds: 120,
        },
      },
    ]);
    assert.ok(account);
    assert.equal(account.config.imap_host, "");
    assert.equal(account.config.smtp_port, port);
  }
});
