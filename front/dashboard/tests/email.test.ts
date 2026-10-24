import { test } from "node:test";
import assert from "node:assert/strict";
import { parseAccounts, connectionUpdate, can } from "../app/utils/email.ts";

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

test("credential replacements are explicit and must be complete", () => {
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
  assert.deepEqual(
    connectionUpdate(account, "login", "new-password").credentials,
    { username: "login", password: "new-password" },
  );
});
