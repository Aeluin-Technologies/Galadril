import assert from "node:assert/strict";
import { chromium } from "@playwright/test";

// Prove that the fixture requires explicit trust rather than a TLS bypass.
const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  await assert.rejects(
    () => page.goto(process.env.GALADRIL_TEST_ORIGIN),
    /net::ERR_CERT_AUTHORITY_INVALID/,
  );
  console.log("Chromium rejected the untrusted fixture certificate.");
} finally {
  await browser.close();
}
