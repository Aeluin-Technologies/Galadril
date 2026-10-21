import { test as base, expect, type Page } from "@playwright/test";
import { setTimeout } from "node:timers/promises";

export const password = "OnlyForBrowserIntegration-9347!";
export const tokenCookie = "__Host-galadril-token";
let remainingRequests = Infinity;
let resetAt = 0;
export const test = base.extend({
  page: async ({ page }, use) => {
    // Scenarios share one source IP; reserve a complete login's budget before starting.
    if (remainingRequests < 30) {
      const delay = Math.max(0, resetAt - Date.now() + 100);
      expect(delay).toBeLessThanOrEqual(60_100);
      if (delay) await setTimeout(delay);
      remainingRequests = Infinity;
    }
    const errors: string[] = [];
    const throttled: string[] = [];
    const browserTelemetry: string[] = [];
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
      if (!new URL(response.url()).pathname.startsWith("/auth/")) return;
      if (response.status() === 429)
        throttled.push(new URL(response.url()).pathname);
      const headers = response.headers();
      const nextReset = Number(headers["x-ratelimit-reset"]);
      const remaining = Number(headers["x-ratelimit-remaining"]);
      if (!Number.isFinite(nextReset) || !Number.isFinite(remaining)) return;
      if (nextReset > resetAt) {
        resetAt = nextReset;
        remainingRequests = remaining;
      } else if (nextReset === resetAt)
        remainingRequests = Math.min(remainingRequests, remaining);
    });
    await page.addInitScript(() => {
      if (window === window.top)
        window.documentIdentity = crypto.randomUUID();
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
declare global { interface Window { documentIdentity: string; } }
