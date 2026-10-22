import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  renderPage,
  renderLua,
  contentSecurityPolicy,
  updateConfigurations,
  root,
} from "../build-error-page.mjs";

test("the outage page is self-contained and uses canonical UI tokens", () => {
  const page = renderPage();
  assert.match(page, /<html lang="en">/);
  assert.match(page, /data:image\/webp;base64,/);
  assert.doesNotMatch(page, /<(?:script|iframe)|@import|(?:src|href)="https?:/);
  assert.match(page, /Service unavailable/);
  assert.match(page, /Trace reference<\/dt>/);
  for (const variable of [
    "--ui-foreground",
    "--ui-canvas",
    "--ui-primary",
    "--ui-radius-control",
  ])
    assert.ok(page.includes(variable));
  const css = readFileSync(
    resolve(root, "front/ui/src/tokens.css"),
    "utf8",
  ).match(/^:root \{[\s\S]*?\n\}/)[0];
  assert.ok(page.includes(css));
  const policy = contentSecurityPolicy(page);
  assert.match(policy, /script-src 'none'/);
  assert.match(policy, /style-src 'sha256-/);
  assert.doesNotMatch(policy, /unsafe-inline|unsafe-eval/);
});

test("committed Envoy and Kubernetes pages match their shared sources", () => {
  updateConfigurations(true);
  const lua = renderLua();
  assert.doesNotMatch(lua, /@@HTML@@|@@CSP@@|@@TOKENS@@|@@LOGO@@/);
  assert.match(lua, /html_escape\(values\[key\]/);
});

test("standalone proxies load the generated outage filter from a file", () => {
  const lua = renderLua();
  assert.equal(
    readFileSync(resolve(root, "infrastructure/envoy/error-page.lua"), "utf8"),
    lua,
  );
  for (const profile of ["local", "gateway"]) {
    const config = readFileSync(
      resolve(root, `infrastructure/envoy/${profile}.yaml`),
      "utf8",
    );
    assert.match(
      config,
      /default_source_code:\n\s+filename: \/etc\/envoy\/error-page\.lua/,
    );
    assert.doesNotMatch(
      config,
      /data:image\/webp;base64,|BEGIN GENERATED ERROR PAGE/,
    );
  }
});
