import { test as base, expect } from "@playwright/test";
import { setTimeout } from "node:timers/promises";

const password = "OnlyForBrowserIntegration-9347!";
const tokenCookie = "__Host-galadril-token";
let remainingRequests = Infinity;
let resetAt = 0;
const test = base.extend({
  page: async ({ page }, use) => {
    // Scenarios share one source IP; reserve a complete login's budget before starting.
    if (remainingRequests < 30) {
      const delay = Math.max(0, resetAt - Date.now() + 100);
      expect(delay).toBeLessThanOrEqual(60_100);
      if (delay) await setTimeout(delay);
      remainingRequests = Infinity;
    }
    const errors = [];
    const throttled = [];
    const browserTelemetry = [];
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

async function emailStep(page, actor) {
  await page.goto("/login");
  await page
    .getByLabel("Work email", { exact: true })
    .fill(`${actor}@demo.invalid`);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByLabel("Password", { exact: true })).toBeVisible();
}

async function finishLogin(page, actor) {
  await emailStep(page, actor);
  await page.getByLabel("Password", { exact: true }).fill(password);
  const submitted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname === "/identity/self-service/login",
  );
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const fields = new URLSearchParams((await submitted).postData());
  expect(fields.get("method")).toBe("password");
  await expect(page).toHaveURL(/\/login\?mfa=1$/);
  await page
    .getByRole("button", { name: "Create passkey", exact: true })
    .click();
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN).href,
  );
  await expect(page.getByLabel("Account menu")).toBeVisible();
}

async function administration(page, actor) {
  await finishLogin(page, actor);
  await page.getByLabel("Account menu").click();
  await page.getByRole("link", { name: "Administration", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Your organization", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Organization name", { exact: true }),
  ).toHaveValue("Browser test organization");
}

async function section(page, name) {
  await page
    .getByRole("navigation", { name: "Organization settings" })
    .getByRole("link", { name })
    .click();
}

function notice(page) {
  return page
    .getByRole("region", { name: "Administration", exact: true })
    .getByRole("status");
}

function observeMutations(page) {
  const operations = [];
  page.on("request", (request) => {
    if (
      new URL(request.url()).pathname !== "/graphql" ||
      request.method() !== "POST"
    )
      return;
    const body = request.postDataJSON();
    if (body.query?.startsWith("mutation")) operations.push(body);
  });
  return operations;
}

test("anonymous pages are protected and login asks only for an email", async ({
  page,
  context,
}) => {
  for (const path of ["/", "/settings/sso#users"]) {
    await page.goto(path);
    await expect(page).toHaveURL((url) => url.pathname === "/login");
    await expect(
      page.getByRole("heading", { name: "Sign in to Galadril" }),
    ).toBeVisible();
    await expect(page.locator("input:visible")).toHaveCount(1);
    await expect(page.getByRole("button")).toHaveText(["Continue"]);
  }
  expect(
    (await context.cookies()).some((cookie) => cookie.name === tokenCookie),
  ).toBe(false);
});

test("password and passkey transitions keep the document, session survives reload, logout protects it", async ({
  page,
  context,
}) => {
  await page.goto("/login");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  await page
    .getByLabel("Work email", { exact: true })
    .fill("browser-auth@demo.invalid");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const field = page.getByLabel("Password", { exact: true });
  await expect(field).toBeFocused();
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  await field.fill("Incorrect-password-123!");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(
    (await context.cookies()).some((cookie) => cookie.name === tokenCookie),
  ).toBe(false);
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  await field.fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const create = page.getByRole("button", {
    name: "Create passkey",
    exact: true,
  });
  await expect(create).toBeEnabled();
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  expect(
    (await context.cookies()).some((cookie) => cookie.name === tokenCookie),
  ).toBe(false);
  await create.click();
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN).href,
  );
  const cookie = (await context.cookies()).find(
    (value) => value.name === tokenCookie,
  );
  expect(cookie).toMatchObject({
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
    path: "/",
  });
  await expect(
    page.getByRole("link", { name: "Galadril Platform", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", {
      name: "Deploy your data foundations.",
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Skip tour", exact: true }).click();
  expect(
    await page.evaluate(() => document.cookie.includes("galadril-token")),
  ).toBe(false);
  const claims = JSON.parse(
    Buffer.from(cookie.value.split(".")[1], "base64url").toString(),
  );
  expect(claims).toMatchObject({
    sub: "browser-auth",
    tenant_id: "demo",
    token_use: "access",
    acr: "urn:galadril:webauthn:uv",
  });
  expect(claims.amr).toEqual(expect.arrayContaining(["pwd", "mfa", "pop"]));
  expect(
    await page.evaluate(() =>
      [...Object.values(localStorage), ...Object.values(sessionStorage)].some(
        (value) => value.includes("eyJ"),
      ),
    ),
  ).toBe(false);
  await page.reload();
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN).href,
  );
  await page.getByLabel("Account menu").click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page).toHaveURL(/\/login$/);
  expect(
    (await context.cookies()).some((value) => value.name === tokenCookie),
  ).toBe(false);
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
  await emailStep(page, "browser-auth");
  await field.fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.getByRole("button", { name: "Use passkey", exact: true }).click();
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN).href,
  );
});

test("enterprise email follows real Polis OIDC and requires passkey enrollment", async ({
  page,
  context,
}) => {
  await page.goto("/login");
  await page
    .getByLabel("Work email", { exact: true })
    .fill("alice@acme.example");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page).toHaveURL(/\/login\?mfa=1$/);
  await page
    .getByRole("button", { name: "Create passkey", exact: true })
    .click();
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN).href,
  );
  const cookie = (await context.cookies()).find(
    (value) => value.name === tokenCookie,
  );
  const claims = JSON.parse(
    Buffer.from(cookie.value.split(".")[1], "base64url").toString(),
  );
  expect(claims.tenant_id).toBe("acme");
  expect(claims.amr).toEqual(
    expect.arrayContaining(["federated", "mfa", "pop"]),
  );
});

test("ordinary members cannot open administration, including a direct navigation", async ({
  page,
}) => {
  await finishLogin(page, "browser-member");
  await page.getByLabel("Account menu").click();
  await expect(
    page.getByRole("link", { name: "Administration", exact: true }),
  ).toHaveCount(0);
  await page.goto("/settings/sso");
  await expect(
    page.getByText("Administrator access required", { exact: false }).first(),
  ).toBeVisible();
  await expect(
    page.getByLabel("Organization name", { exact: true }),
  ).toHaveCount(0);
});

test("organization opens first and people actions require confirmation", async ({
  page,
}) => {
  await administration(page, "browser-people");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Your organization", exact: true }),
  ).toBeVisible();
  const mutations = observeMutations(page);
  await section(page, "People");
  await page
    .getByRole("button", { name: "Manage Browser administrator", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Suspend access", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await page
    .getByRole("button", { name: "Manage Alice Example", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Suspend access", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Suspend this person?", exact: true }),
  ).toBeVisible();
  expect(mutations).toHaveLength(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page
    .getByRole("button", { name: "Reset passkey", exact: true })
    .click();
  expect(mutations).toHaveLength(0);
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(notice(page)).toContainText("Passkey reset");
  expect(mutations).toHaveLength(1);
  expect(mutations[0].variables).toEqual({ user: "alice" });
  await page
    .getByRole("button", { name: "Suspend access", exact: true })
    .click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Restore access", exact: true }),
  ).toBeVisible();
  expect(mutations.at(-1).variables).toEqual({ user: "alice", active: false });
});

test("SSO normalizes email and applies detected settings only after consent", async ({
  page,
}) => {
  await administration(page, "browser-provider");
  const mutations = observeMutations(page);
  await section(page, "Sign-in & security");
  await page
    .getByLabel("Company email domain", { exact: true })
    .fill("Alex@Example.COM");
  await page
    .getByRole("button", { name: "Get verification instructions", exact: true })
    .click();
  await expect(
    page.getByLabel("Company email domain", { exact: true }),
  ).toHaveValue("example.com");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Provider or metadata address", { exact: true })
    .fill("login.example.com");
  await page
    .getByRole("button", { name: "Detect configuration", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Detected provider" }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Company sign-in address", { exact: true }),
  ).toHaveValue("");
  expect(
    mutations.some((value) => value.query.includes("configureSsoConnection")),
  ).toBe(false);
  await page
    .getByRole("button", { name: "Use detected settings", exact: true })
    .click();
  await expect(
    page.getByLabel("Company sign-in address", { exact: true }),
  ).toHaveValue("https://login.example.com");
  await page
    .getByText("My provider requires app credentials", { exact: true })
    .click();
  await expect(
    page.getByLabel("Application client secret", { exact: true }),
  ).toHaveValue("");
  await page
    .getByRole("button", { name: "Review connection", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Ready to connect your team?",
      exact: true,
    }),
  ).toBeVisible();
  expect(
    mutations.some((value) => value.query.includes("configureSsoConnection")),
  ).toBe(false);
  await page
    .getByRole("button", { name: "Verify and connect", exact: true })
    .click();
  await expect(notice(page)).toContainText("Connection saved");
  const saved = mutations.find((value) =>
    value.query.includes("configureSsoConnection"),
  );
  expect(JSON.parse(saved.variables.configuration)).toEqual({
    domain: "example.com",
    oidc_issuer: "https://login.example.com",
    jit_provision: false,
  });
});

test("people reload uses an animated icon and announces completion without replacing the document", async ({
  page,
}) => {
  await administration(page, "browser-people-refresh");
  await section(page, "People");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  let release;
  const responseGate = new Promise((resolve) => {
    release = resolve;
  });
  await page.route("**/graphql", async (route) => {
    if (route.request().postDataJSON().query.includes("users("))
      await responseGate;
    await route.continue();
  });
  const reload = page.getByRole("button", {
    name: "Reload people",
    exact: true,
  });
  await reload.click();
  await expect(reload).toHaveAttribute("aria-busy", "true");
  await expect(reload.locator("svg")).toHaveClass(/animate-spin/);
  release();
  await expect(notice(page)).toContainText("People updated");
  await expect(reload).toHaveAttribute("aria-busy", "false");
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
});

test("permission blocks round-trip through code and require opt-in and review", async ({
  page,
}) => {
  await administration(page, "browser-permissions");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  const mutations = observeMutations(page);
  await section(page, "Permissions");
  await page
    .getByRole("button", {
      name: "Import permissions automatically",
      exact: false,
    })
    .click();
  await page
    .getByText("Import a configuration · advanced", { exact: true })
    .click();
  await expect(
    page.getByLabel("Allow permission imports", { exact: true }),
  ).not.toBeChecked();
  await page
    .getByRole("button", { name: "+ Assign a role", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Person", exact: true })
    .selectOption("alice");
  await page
    .getByRole("combobox", { name: "Role", exact: true })
    .selectOption("reader");
  await page.getByRole("button", { name: "Code", exact: true }).click();
  const source = await page
    .getByLabel("Permission manifest", { exact: true })
    .inputValue();
  expect(JSON.parse(source).operations).toHaveLength(1);
  await page
    .getByRole("button", { name: "Visual blocks", exact: true })
    .click();
  await expect(
    page.getByRole("combobox", { name: "Person", exact: true }),
  ).toHaveValue("alice");
  await expect(
    page.getByRole("button", { name: "Review changes", exact: true }),
  ).toBeDisabled();
  expect(mutations).toHaveLength(0);
  await page.getByLabel("Allow permission imports", { exact: true }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Review changes", exact: true }),
  ).toBeEnabled();
  await page
    .getByRole("button", { name: "Review changes", exact: true })
    .click();
  expect(
    mutations.filter((value) => value.query.includes("importPermissions")),
  ).toHaveLength(0);
  const applied = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/graphql" &&
      response.request().postDataJSON()?.query.includes("importPermissions"),
  );
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  const imported = await applied;
  expect(imported.status()).toBe(200);
  const result = await imported.json();
  expect(JSON.parse(result.data.administration.importPermissions)).toEqual({
    applied: 1,
    complete: true,
    failed_at: null,
  });
  await expect(notice(page)).toContainText("1 access changes applied");
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  expect(JSON.parse(mutations.at(-1).variables.manifest)).toEqual(
    JSON.parse(source),
  );
  await page.getByRole("button", { name: "Code", exact: true }).click();
  await page
    .getByLabel("Permission manifest", { exact: true })
    .fill('{"version":99,"operations":[]}');
  await page
    .getByRole("button", { name: "Visual blocks", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("cannot be represented");
  await expect(
    page.getByLabel("Permission manifest", { exact: true }),
  ).toHaveValue('{"version":99,"operations":[]}');
});

test("permissions guide administrators through choices, filtered access maps and UTC rules without reloading", async ({
  page,
}, testInfo) => {
  await administration(page, "browser-guided-permissions");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  const mutations = observeMutations(page);
  await section(page, "Permissions");
  await expect(
    page.getByRole("button", {
      name: "Import permissions automatically",
      exact: false,
    }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Manage roles and people", exact: false })
    .click();
  await expect(
    page.getByRole("region", { name: "Access map", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("region", { name: "Access map", exact: true })
    .screenshot({
      path: testInfo.outputPath("access-map.png"),
    });
  await page
    .getByLabel("Who needs access?", { exact: true })
    .selectOption("role:reader");
  const permission = page.getByLabel("Permission", { exact: true });
  const grant = page.getByRole("button", {
    name: "Grant data access",
    exact: true,
  });
  const remove = page.getByRole("button", {
    name: "Remove data access",
    exact: true,
  });
  await expect(permission).toBeDisabled();
  await expect(permission).toContainText("Choose a data resource first");
  await expect(grant).toBeDisabled();
  await expect(remove).toBeDisabled();
  await page
    .getByLabel("Data resource", { exact: true })
    .selectOption("document:report");
  await expect(permission).toBeEnabled();
  await expect(permission).toHaveValue("reader");
  await expect(grant).toBeEnabled();
  await expect(remove).toBeEnabled();
  await page.getByText("Create a role", { exact: true }).click();
  await page.getByLabel("New role name", { exact: true }).fill("analyst");
  await page.getByRole("button", { name: "Create role", exact: true }).click();
  await expect(notice(page)).toContainText("Role created");
  await page
    .getByLabel("Who needs access?", { exact: true })
    .selectOption("role:analyst");
  await page
    .getByLabel("Role to include", { exact: true })
    .selectOption("reader");
  await page.getByRole("button", { name: "Include role", exact: true }).click();
  await expect(notice(page)).toContainText("Role inheritance updated");
  expect(mutations.at(-1).variables).toEqual({
    role: "reader",
    parent: "analyst",
    enabled: true,
  });
  await page.screenshot({
    path: testInfo.outputPath("guided-permissions.png"),
    fullPage: true,
  });
  expect(mutations.at(-1).query).toContain("administration");
  await page
    .getByRole("button", { name: "All permission options", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Set general access rules", exact: false })
    .click();
  await page.getByLabel("Rule scope", { exact: true }).selectOption("reader");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Access starts at (UTC)", { exact: true })
    .fill("08:00");
  await page.getByLabel("Access ends at (UTC)", { exact: true }).fill("18:00");
  await page.getByRole("button", { name: "Review rule", exact: true }).click();
  await expect(
    page.getByText("08:00–18:00 UTC", { exact: false }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("general-access-review.png"),
    fullPage: true,
  });
  expect(
    mutations.some((mutation) =>
      mutation.query.includes("configureGeneralAccess"),
    ),
  ).toBe(false);
  await page.getByRole("button", { name: "Save rule", exact: true }).click();
  await expect(notice(page)).toContainText("General access rule saved");
  expect(JSON.parse(mutations.at(-1).variables.configuration)).toEqual({
    roleNames: ["reader"],
    startHourUtc: 8,
    endHourUtc: 18,
    regions: [],
  });
  expect(mutations.at(-1).variables.id).toBe("office-hours");
  await page.getByLabel("Rule name", { exact: true }).fill("Evening support");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page
    .getByLabel("Access starts at (UTC)", { exact: true })
    .fill("18:00");
  await page.getByLabel("Access ends at (UTC)", { exact: true }).fill("22:00");
  await page.getByRole("button", { name: "Review rule", exact: true }).click();
  await expect(
    page.getByText("Saving creates a new rule evening-support.", {
      exact: false,
    }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save rule", exact: true }).click();
  await expect(
    page.getByText("office-hours", { exact: false }).last(),
  ).toBeVisible();
  await expect(
    page.getByText("evening-support", { exact: false }).last(),
  ).toBeVisible();
  expect(mutations.at(-1).variables.id).toBe("evening-support");
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
});

test("administration errors explain recovery and never display backend details", async ({
  page,
}) => {
  await administration(page, "browser-admin-errors");
  await page.route("**/graphql", async (route) => {
    if (!route.request().postDataJSON().query.includes("updateOrganization"))
      return route.continue();
    await route.fulfill({
      json: {
        errors: [
          {
            message: "private postgres connection password=secret",
            extensions: { code: "FORBIDDEN" },
          },
        ],
      },
    });
  });
  await page
    .getByLabel("Organization name", { exact: true })
    .fill("Changed team");
  await page.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("administrator");
  await expect(page.getByRole("alert")).not.toContainText("password");
});

test("enabling passkey protection verifies immediately in a modal and preserves the current page", async ({
  page,
  context,
}) => {
  await administration(page, "browser-enable-passkey");
  const documentResponse = await page.reload();
  expect(documentResponse.headers()["permissions-policy"]).toMatch(
    /publickey-credentials-get=\(self\)/,
  );
  expect(documentResponse.headers()["permissions-policy"]).toMatch(
    /publickey-credentials-create=\(self\)/,
  );
  await section(page, "Sign-in & security");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  await expect(
    page.getByLabel("Require a passkey for everyone", { exact: true }),
  ).not.toBeChecked();
  await page
    .getByLabel("Require a passkey for everyone", { exact: true })
    .click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  const dialog = page.getByRole("dialog", {
    name: "Secure your account",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await expect(dialog).toHaveJSProperty("open", true);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  expect(
    await page.evaluate(() =>
      Boolean(document.activeElement?.closest("dialog")),
    ),
  ).toBe(true);
  await expect(page).toHaveURL(/\/settings\/sso#domains$/);
  expect(
    (await context.cookies()).some((cookie) => cookie.name === tokenCookie),
  ).toBe(true);
  await dialog
    .getByRole("button", { name: "Verify passkey", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  await expect(notice(page)).toContainText("Sign-in security updated");
  await expect(
    page.getByLabel("Require a passkey for everyone", { exact: true }),
  ).toBeChecked();
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
});

test("an active visible dashboard periodically renews its session using protected cookies", async ({
  page,
  context,
}) => {
  await page.clock.install();
  await finishLogin(page, "browser-session-renewal");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  const renewed = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === "/auth/refresh" &&
      response.request().method() === "POST",
  );
  await page.clock.fastForward(61_000);
  const response = await renewed;
  expect(response.status()).toBe(200);
  expect(await response.json()).toEqual({ expiresAt: expect.any(Number) });
  const cookie = (await context.cookies()).find(
    (value) => value.name === tokenCookie,
  );
  expect(cookie).toMatchObject({
    httpOnly: true,
    secure: true,
    sameSite: "Lax",
    path: "/",
  });
  expect(
    await page.evaluate(() => document.cookie.includes("galadril-token")),
  ).toBe(false);
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  await expect(page).toHaveURL(
    new URL("/", process.env.GALADRIL_TEST_ORIGIN).href,
  );
  await page.route("**/auth/refresh", (route) =>
    route.fulfill({ status: 503, json: { statusCode: 503 } }),
  );
  const unavailable = page.waitForResponse(
    (result) =>
      new URL(result.url()).pathname === "/auth/refresh" &&
      result.status() === 503,
  );
  await page.clock.fastForward(61_000);
  await unavailable;
  await expect(
    page.getByRole("status").filter({ hasText: "You are still signed in" }),
  ).toBeVisible();
  await expect(page.getByLabel("Account menu")).toBeVisible();
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  await page.unroute("**/auth/refresh");
  const recovered = page.waitForResponse(
    (result) =>
      new URL(result.url()).pathname === "/auth/refresh" &&
      result.status() === 200,
  );
  await page.clock.fastForward(61_000);
  await recovered;
  await expect(
    page.getByRole("status").filter({ hasText: "You are still signed in" }),
  ).not.toBeVisible();
});

test("access-token expiry restores the signed session during navigation and reload", async ({
  page,
  context,
}) => {
  await finishLogin(page, "browser-session-restoration");
  const documentIdentity = await page.evaluate(() => window.documentIdentity);
  const restore = () =>
    page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/auth/refresh" &&
        response.request().method() === "POST",
    );
  await context.clearCookies({ name: tokenCookie });
  const navigated = restore();
  await page.getByLabel("Account menu").click();
  await page.getByRole("link", { name: "Administration", exact: true }).click();
  expect((await navigated).status()).toBe(200);
  await expect(
    page.getByRole("heading", { name: "Your organization", exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => window.documentIdentity)).toBe(
    documentIdentity,
  );
  await context.clearCookies({ name: tokenCookie });
  const reloaded = restore();
  await page.reload();
  expect((await reloaded).status()).toBe(200);
  await expect(page).toHaveURL(/\/settings\/sso$/);
  await expect(page.getByLabel("Account menu")).toBeVisible();
  expect(
    (await context.cookies()).some((cookie) => cookie.name === tokenCookie),
  ).toBe(true);
  const renewal = (await context.cookies()).find(
    (cookie) => cookie.name === "__Host-galadril-renewal",
  );
  expect(renewal).toBeDefined();
  await context.addCookies([{ ...renewal, value: "malformed" }]);
  await context.clearCookies({ name: tokenCookie });
  await page.reload();
  await expect(page).toHaveURL(/\/login$/);
  expect(
    (await context.cookies()).some((cookie) => cookie.name === tokenCookie),
  ).toBe(false);
});

test("administration remains scrollable on a narrow screen", async ({
  page,
}) => {
  await administration(page, "browser-mobile");
  await page.setViewportSize({ width: 390, height: 640 });
  await section(page, "Sign-in & security");
  await page
    .getByLabel("Company email domain", { exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    page.getByLabel("Company email domain", { exact: true }),
  ).toBeInViewport();
  expect(
    await page
      .getByRole("region", { name: "Administration", exact: true })
      .evaluate(
        (element) =>
          element.scrollHeight > element.clientHeight && element.scrollTop > 0,
      ),
  ).toBe(true);
});
