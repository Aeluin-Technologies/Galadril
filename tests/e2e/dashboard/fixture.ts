import { test as base, expect, type Page } from "@playwright/test";
import { setTimeout } from "node:timers/promises";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { AuthenticationBudget } from "./authentication-budget.ts";
import assert from "node:assert/strict";

/** Missing fixture data must fail explicitly instead of weakening strict null checks. */
export function required<T>(value: T | null | undefined): T {
  assert.ok(
    value !== null && value !== undefined,
    "Required fixture value is missing",
  );
  return value;
}

export const password = "OnlyForBrowserIntegration-9347!";
export const tokenCookie = "__Host-galadril-token";
export const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    mkdirSync(testInfo.project.outputDir, { recursive: true });
    const budget = new AuthenticationBudget(
      join(testInfo.project.outputDir, "authentication-budget.json"),
    );
    const delay = budget.delay();
    if (delay) await setTimeout(delay);
    const errors: string[] = [];
    const throttled: string[] = [];
    const browserTelemetry: string[] = [];
    const authResponses: { path: string; method: string; status: number }[] =
      [];
    const authFailures: { path: string; error: string }[] = [];
    const authenticationPath = (path: string) =>
      path.startsWith("/auth/") ||
      path.startsWith("/identity/") ||
      path === "/oauth2/auth";
    page.on("requestfailed", (request) => {
      const path = new URL(request.url()).pathname;
      if (authenticationPath(path) && authFailures.length < 64)
        authFailures.push({
          path,
          error: request.failure()?.errorText ?? "unknown",
        });
    });
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        /^\/v1\/(metrics|traces|logs)$/.test(url.pathname) ||
        ["4317", "4318"].includes(url.port)
      )
        browserTelemetry.push(url.origin + url.pathname);
    });
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("response", (response) => {
      const path = new URL(response.url()).pathname;
      if (authenticationPath(path) && authResponses.length < 64)
        authResponses.push({
          path,
          method: response.request().method(),
          status: response.status(),
        });
      if (!path.startsWith("/auth/")) return;
      if (response.status() === 429)
        throttled.push(new URL(response.url()).pathname);
      budget.observe(response.headers());
    });
    await page.addInitScript(() => {
      if (window === window.top) window.documentIdentity = crypto.randomUUID();
    });
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("WebAuthn.enable");
    await cdp.send("WebAuthn.addVirtualAuthenticator", {
      options: {
        protocol: "ctap2",
        transport: "internal",
        hasResidentKey: true,
        hasUserVerification: true,
        isUserVerified: true,
        automaticPresenceSimulation: true,
      },
    });
    await use(page);
    if (testInfo.status !== testInfo.expectedStatus)
      console.error(
        JSON.stringify({
          event: "e2e.authentication.diagnostics",
          path: new URL(page.url()).pathname,
          responses: authResponses,
          failures: authFailures,
        }),
      );
    expect(
      throttled,
      "Each scenario must stay within the unchanged auth rate limit",
    ).toEqual([]);
    expect(errors, "Unhandled errors in the compiled Nuxt application").toEqual(
      [],
    );
    expect(browserTelemetry, "Browsers must never export telemetry").toEqual(
      [],
    );
  },
});

export async function emailStep(page: Page, actor: string) {
  await page.goto("/login");
  await page
    .getByRole("textbox", { name: "Work email", exact: true })
    .fill(`${actor}@demo.invalid`);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByLabel(/^Password\s*\*?$/)).toBeVisible();
}

export async function finishLogin(page: Page, actor: string) {
  await emailStep(page, actor);
  await page.getByLabel(/^Password\s*\*?$/).fill(password);
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/identity/self-service/login",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const fields = new URLSearchParams((await submitted).postData() ?? "");
  expect(fields.get("method")).toBe("password");
  await expect(page).toHaveURL(/\/login\?mfa=1$/);
  await page
    .getByRole("button", { name: "Create passkey", exact: true })
    .click();
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN ?? "https://localhost").href,
  );
  await expect(page.getByLabel("Account menu")).toBeVisible();
}

export { expect };
declare global {
  interface Window {
    documentIdentity: string;
  }
}
