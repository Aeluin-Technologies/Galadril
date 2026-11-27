import assert from "node:assert/strict";
import { test } from "node:test";
import { configuration } from "./playwright-ui.config.ts";

test("managed browser fixtures build once and serve production assets on their reserved port", () => {
  const config = configuration({ GALADRIL_UI_TEST_PORT: "32187" });
  assert.equal(config.use?.baseURL, "http://127.0.0.1:32187");
  const server = config.webServer;
  assert.ok(server && !Array.isArray(server));
  assert.match(server.command, /nuxt\.mjs build --envName ui-test/);
  assert.match(server.command, /&& node \.output\/server\/index\.mjs$/);
  assert.doesNotMatch(server.command, /nuxt\.mjs dev/);
  assert.equal(server.env?.NITRO_PORT, "32187");
  assert.equal(server.env?.NITRO_HOST, "127.0.0.1");
  assert.equal(server.env?.NODE_ENV, "production");
  assert.ok((server.timeout ?? 0) >= 240_000);
});

test("an explicitly supplied origin keeps ownership of its server", () => {
  const config = configuration({
    GALADRIL_UI_TEST_ORIGIN: "https://localhost:8080",
  });
  assert.equal(config.webServer, undefined);
  assert.equal(config.use?.baseURL, "https://localhost:8080");
  assert.equal(config.use?.ignoreHTTPSErrors, true);
});
