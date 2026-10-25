import assert from "node:assert/strict";
import { test } from "node:test";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { tmpdir } from "node:os";
import {
  renderPage,
  contentSecurityPolicy,
  root,
} from "../build-error-page.ts";

const require = createRequire(resolve(root, "front/dashboard/package.json"));
const {
  chromium,
}: typeof import("@playwright/test") = require("@playwright/test");
const {
  default: AxeBuilder,
}: typeof import("@axe-core/playwright") = require("@axe-core/playwright");

const details =
  "upstream connect error or disconnect/reset before headers. reset reason: remote connection failure";
const trace = "7b6f80328d9d4cf4a92a86d9555acdf2";
const values: Record<string, string> = {
  STATUS: "503",
  DETAILS: details,
  TRACE_ROW: `<dt>Trace ID</dt><dd>${trace}</dd>`,
  REQUEST_ID: "1b1bb378-a7da-4c31-9e31-a803ea26bb92",
};
const pageHtml = renderPage().replace(
  /@@([A-Z_]+)@@/g,
  (_match: string, key: string) => values[key] ?? "",
);

test("the static outage page works without JavaScript or upstream assets on desktop and mobile", async () => {
  const server = createServer((_request, response) => {
    response.writeHead(503, {
      "content-type": "text/html; charset=utf-8",
      "content-security-policy": contentSecurityPolicy(pageHtml),
      "cache-control": "no-store",
    });
    response.end(pageHtml);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const browser = await chromium.launch();
  try {
    const address = server.address();
    assert.ok(address && typeof address !== "string");
    const port = address.port;
    const url = `http://127.0.0.1:${port}/unavailable`;
    const context = await browser.newContext({ javaScriptEnabled: false });
    const page = await context.newPage();
    const origins: string[] = [];
    page.on("request", (request) =>
      origins.push(new URL(request.url()).origin),
    );
    const destination = resolve(
      process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? tmpdir(),
      "error-page",
    );
    mkdirSync(destination, { recursive: true });
    for (const [name, width, height] of [
      ["desktop", 1440, 1000],
      ["mobile", 390, 844],
    ] satisfies [string, number, number][]) {
      await page.setViewportSize({ width, height });
      await page.goto(url);
      assert.equal(
        await page
          .getByRole("heading", { name: "Service unavailable." })
          .isVisible(),
        true,
      );
      assert.equal(await page.locator("pre").innerText(), details);
      assert.equal(
        await page.getByText(trace, { exact: true }).isVisible(),
        true,
      );
      assert.equal(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      assert.equal(
        await page
          .locator("body")
          .evaluate((element) => getComputedStyle(element).backgroundColor),
        "rgb(248, 250, 252)",
      );
      await page.screenshot({
        path: resolve(destination, `service-unavailable-${name}.png`),
        fullPage: true,
      });
    }
    assert.equal(
      await page.getByRole("link", { name: "Try again" }).count(),
      0,
    );
    assert.equal(await page.getByRole("button").count(), 0);
    assert.ok(origins.every((origin) => origin === new URL(url).origin));
    await context.close();
    // Axe needs script injection; the separate context above checks the actual script-free response.
    const inspection = await browser.newContext({ bypassCSP: true });
    const accessible = await inspection.newPage();
    await accessible.goto(url);
    const result = await new AxeBuilder({ page: accessible })
      .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
      .analyze();
    assert.deepEqual(result.violations, []);
    await inspection.close();
  } finally {
    await browser.close();
    await new Promise<void>((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
});
