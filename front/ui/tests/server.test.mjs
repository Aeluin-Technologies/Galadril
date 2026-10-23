import assert from "node:assert/strict";
import { test } from "node:test";

async function configuration(origin) {
  const previousOrigin = process.env.GALADRIL_UI_TEST_ORIGIN;
  const previousPort = process.env.GALADRIL_UI_TEST_PORT;
  process.env.GALADRIL_UI_TEST_PORT = "32187";
  if (origin) process.env.GALADRIL_UI_TEST_ORIGIN = origin;
  else delete process.env.GALADRIL_UI_TEST_ORIGIN;
  try {
    return (
      await import(
        `./playwright.config.ts?origin=${encodeURIComponent(origin ?? "local")}`
      )
    ).default;
  } finally {
    if (previousOrigin === undefined)
      delete process.env.GALADRIL_UI_TEST_ORIGIN;
    else process.env.GALADRIL_UI_TEST_ORIGIN = previousOrigin;
    if (previousPort === undefined) delete process.env.GALADRIL_UI_TEST_PORT;
    else process.env.GALADRIL_UI_TEST_PORT = previousPort;
  }
}

test("managed browser fixtures build once and serve production assets on their reserved port", async () => {
  const config = await configuration();
  assert.equal(config.use.baseURL, "http://127.0.0.1:32187");
  const server = config.webServer;
  assert.match(server.command, /nuxt\.mjs build playground/);
  assert.match(server.command, /&& node playground\/\.output\/server\/index\.mjs$/);
  assert.doesNotMatch(server.command, /nuxt\.mjs dev/);
  assert.equal(server.env.NITRO_PORT, "32187");
  assert.equal(server.env.NITRO_HOST, "127.0.0.1");
  assert.equal(server.env.NODE_ENV, "production");
  assert.ok(server.timeout >= 240_000);
});

test("an explicitly supplied origin keeps ownership of its server", async () => {
  const config = await configuration("https://localhost:8080");
  assert.equal(config.webServer, undefined);
  assert.equal(config.use.baseURL, "https://localhost:8080");
});
