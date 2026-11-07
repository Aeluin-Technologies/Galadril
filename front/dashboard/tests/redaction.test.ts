import assert from "node:assert/strict";
import { test } from "node:test";
import {
  buildRedactionRules,
  redactLocalDocument,
} from "../app/utils/pengolo-redaction.ts";

test("private matching rules stay declarative and bounded", () => {
  const rules = buildRedactionRules({
    emails: true,
    phones: true,
    addresses: false,
    names: "Monsieur Durand\nJean-Daniel Durand",
    pattern: "",
  });
  assert.equal(rules.length, 4);
  assert.deepEqual(rules[2], {
    kind: "literal",
    pattern: "Monsieur Durand",
    replacement_mode: "initials",
  });
  assert.throws(() =>
    buildRedactionRules({
      emails: false,
      phones: false,
      addresses: false,
      names: "",
      pattern: "",
    }),
  );
  assert.throws(() =>
    buildRedactionRules({
      emails: true,
      phones: true,
      addresses: true,
      names: Array.from({ length: 16 }, (_, i) => `Person ${i}`).join("\n"),
      pattern: "",
    }),
  );
});

test("the local redaction path never uploads an original into the chat or library", async () => {
  const file = new File(["%PDF-1.7 private-source"], "private-filename.pdf", {
    type: "application/pdf",
  });
  let calls = 0;
  const result = await redactLocalDocument(
    file,
    [{ kind: "email" }],
    new AbortController().signal,
    async (url, init) => {
      calls++;
      assert.equal(url, "/pengolo/files/redact");
      assert.equal(init?.credentials, "same-origin");
      assert.equal(init?.cache, "no-store");
      assert.equal(init?.method, "POST");
      assert.ok(!String(init?.body).includes("private-filename"));
      return Response.json({
        mime: "application/pdf",
        content_base64: btoa("%PDF-1.7 redacted-copy"),
        match_count: 1,
        pages: 1,
        requires_review: true,
        layout_preserved: true,
        coverage: "provided_rules_only",
        warnings: [],
      });
    },
  );
  assert.equal(calls, 1);
  assert.equal(result.pages, 1);
  assert.equal(await result.blob.text(), "%PDF-1.7 redacted-copy");
});

test("client validation rejects unsafe or misleading redaction responses", async () => {
  const valid = {
    mime: "application/pdf",
    content_base64: btoa("%PDF-1.7 copy"),
    match_count: 1,
    pages: 1,
    requires_review: true,
    layout_preserved: true,
    coverage: "provided_rules_only",
    warnings: [],
  };
  const file = new File(["%PDF-1.7 source"], "source.pdf", {
    type: "application/pdf",
  });
  for (const patch of [
    { requires_review: false },
    { layout_preserved: false },
    { coverage: "all_personal_data" },
    { mime: "text/html" },
    { pages: 129 },
    { content_base64: btoa("<html>unsafe</html>") },
  ]) {
    await assert.rejects(
      redactLocalDocument(
        file,
        [{ kind: "email" }],
        new AbortController().signal,
        async () => Response.json({ ...valid, ...patch }),
      ),
    );
  }
});
