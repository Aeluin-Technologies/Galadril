import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { parse, Kind } from "graphql";

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (error) => console.error(error.stack));
  page.on("console", (message) => {
    if (message.type() === "error") console.error(message.text());
  });
});

async function mockDashboard(page: Page, libraryItems: object[] = []) {
  const shell = await page.request.get("/login");
  for (const path of ["/pengolo", "/settings/sso"])
    await page.route(`**${path}`, (route) =>
      route.request().isNavigationRequest()
        ? route.fulfill({ response: shell })
        : route.continue(),
    );
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        subject: "ui-user",
        tenant: "ui-tenant",
        displayName: "Alex Morgan",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        requiresPasskey: false,
      },
    }),
  );
  await page.route("**/auth/start", (route) =>
    route.fulfill({
      json: { local: true, redirect: "/oauth2/auth?fixture=ui" },
    }),
  );
  await page.route("**/oauth2/auth?fixture=ui", (route) =>
    route.fulfill({ status: 302, headers: { location: "/login?flow=ui" } }),
  );
  await page.route("**/login?flow=ui", (route) =>
    route.request().isNavigationRequest()
      ? route.continue()
      : route.fulfill({ body: "Flow prepared" }),
  );
  await page.route("**/auth/local/flow?flow=ui", (route) =>
    route.fulfill({
      json: {
        action: "/identity/self-service/login?flow=ui",
        method: "post",
        nodes: [
          {
            type: "input",
            attributes: {
              name: "csrf_token",
              type: "hidden",
              value: "fixture-csrf",
            },
            meta: {},
          },
          {
            type: "input",
            attributes: { name: "password", type: "password", required: true },
            meta: { label: { text: "Password" } },
          },
          {
            type: "input",
            attributes: { name: "method", type: "submit", value: "password" },
            meta: {},
          },
        ],
      },
    }),
  );
  await page.route("**/identity/self-service/login?flow=ui", async (route) => {
    const request = route.request();
    const fields = await new Request(request.url(), {
      method: "POST",
      headers: { "content-type": request.headers()["content-type"] ?? "" },
      body: new Uint8Array(request.postDataBuffer() ?? []),
    }).formData();
    expect(fields.get("csrf_token")).toBe("fixture-csrf");
    expect(fields.get("method")).toBe("password");
    await route.fulfill({ status: 200, body: "Fixture sign-in completed" });
  });
  await page.route("**/", (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.fulfill({ body: "Signed in" }),
  );
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage((raw) => {
      if (JSON.parse(String(raw)).type === "connection_init")
        socket.send(JSON.stringify({ type: "connection_ack" }));
    }),
  );
  let personLabel = "Alex";
  let roleLabel = "analyst";
  await page.route("**/graphql", (route) => {
    const payload = route.request().postDataJSON();
    if (payload.query.includes("setDirectoryDisplayName")) {
      expect(payload.variables.id).toBe(
        payload.variables.kind === "role" ? "analyst" : "ui-user",
      );
      if (payload.variables.kind === "role") roleLabel = payload.variables.name;
      else personLabel = payload.variables.name;
      return route.fulfill({
        json: { data: { administration: { setDirectoryDisplayName: true } } },
      });
    }
    if (payload.query.includes("mutation savePengoloProject")) {
      const input = payload.variables.input;
      expect(input.id).toBe("project-fixture");
      return route.fulfill({
        json: {
          data: {
            savePengoloProject: {
              id: input.id,
              ownerId: "ui-user",
              revision: 2,
              data: input,
            },
          },
        },
      });
    }
    const requestedFields = parse(payload.query).definitions.flatMap(
      (definition) =>
        definition.kind === Kind.OPERATION_DEFINITION
          ? definition.selectionSet.selections.flatMap((selection) =>
              selection.kind === Kind.FIELD
                ? [selection.alias?.value ?? selection.name.value]
                : [],
            )
          : [],
    );
    const respond = (response: { json: { data: Record<string, unknown> } }) =>
      route.fulfill({
        json: {
          data: Object.fromEntries(
            requestedFields.map((name) => [
              name,
              response.json.data[name] ?? null,
            ]),
          ),
        },
      });
    return respond({
      json: {
        data: {
          pengoloIdentity: {
            version: "ui",
            administrator: true,
            webSearchAvailable: true,
          },
          pengoloModels: {
            defaultModel: "fixture",
            models: [
              {
                alias: "fixture",
                model: "Fixture model",
                provider: "fixture",
                reasoningEfforts: ["medium"],
              },
            ],
          },
          pengoloProjects: [
            {
              id: "project-fixture",
              ownerId: "ui-user",
              revision: 1,
              data: {
                name: "Operations",
                icon: "📁",
                color: "#d97706",
                shared: false,
                roles: [],
                contextMode: "project",
              },
            },
          ],
          pengoloChats: {
            items: [
              {
                id: "chat-fixture",
                ownerId: "ui-user",
                projectId: null,
                revision: 1,
                data: { title: "Daily review", pinned: false },
              },
            ],
            after: null,
          },
          pengoloLibrary: { items: libraryItems, after: null },
          pengoloAgents: [],
          scribeCapabilities: { capabilities: [] },
          administration: {
            ssoSettings: {
              organizationName: "Galadril",
              organization_name: "Galadril",
              passkey_mfa: false,
              permission_import: false,
              connections: [],
            },
            users: {
              items: [
                {
                  userId: "ui-user",
                  email: "alex@example.test",
                  displayName: personLabel,
                  isActive: true,
                },
              ],
              after: null,
            },
            roles: {
              items: [{ roleName: "analyst", displayName: roleLabel }],
              after: null,
            },
            accessGraph: {
              nodes: [
                {
                  id: "user:ui-user",
                  label: personLabel,
                  kind: "user",
                  resourceId: "ui-user",
                },
                {
                  id: "role:analyst",
                  label: roleLabel,
                  kind: "role",
                  resourceId: "analyst",
                },
                {
                  id: "document:report",
                  label: "Quarterly report",
                  kind: "document",
                  resourceId: "report",
                },
              ],
              edges: [
                {
                  source: "user:ui-user",
                  target: "role:analyst",
                  relation: "member",
                },
                {
                  source: "role:analyst",
                  target: "document:report",
                  relation: "owner",
                },
                {
                  source: "role:analyst",
                  target: "document:report",
                  relation: "reader",
                },
              ],
              truncated: false,
            },
            cedarPolicies: [],
          },
        },
      },
    });
  });
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
}

async function signIn(page: Page) {
  await mockDashboard(page);
  // Playwright routes only the first URL of a redirect chain; the fixture models its terminal response.
  await page.addInitScript(() => {
    const request = window.fetch.bind(window);
    window.fetch = async (input, options) => {
      const response = await request(input, options);
      const target = new URL(
        input instanceof Request ? input.url : String(input),
        location.origin,
      );
      if (
        target.pathname === "/identity/self-service/login" &&
        target.searchParams.get("flow") === "ui" &&
        response.ok
      )
        Object.defineProperty(response, "url", {
          value: `${location.origin}/`,
        });
      return response;
    };
  });
  await page.goto("/login");
  await page
    .getByRole("textbox", { name: "Work email", exact: true })
    .fill("ui@example.test");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.locator('input[name="password"]').fill("Fixture-only-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByLabel("Account menu")).toBeVisible();
}

test("login stays accessible and responsive with shared controls", async ({
  page,
}, info) => {
  await page.goto("/login");
  await expect(
    page.getByRole("heading", { name: "Sign in to Galadril" }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Work email" }),
  ).toHaveAttribute("data-ui", "input");
  await expect(page.getByRole("heading")).toHaveCount(1);
  await expect(page.getByRole("complementary")).toHaveCount(0);
  await expect(page.getByRole("button")).toHaveCount(1);
  await expect(page.getByRole("contentinfo")).toHaveCount(0);
  await expect(
    page.getByText("Your work email. Your organization’s sign-in.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("Your organization manages your credentials and access.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  await expect(page.locator("[data-auth-decoration]")).toHaveAttribute(
    "aria-hidden",
    "true",
  );
  await expect(
    page.getByText("Your organization manages your credentials and access.", {
      exact: true,
    }),
  ).toHaveCSS("font-size", "12px");
  const panel = page.getByRole("region", { name: "Sign in to Galadril" });
  await expect(panel).toHaveCSS("border-top-width", "1px");
  expect(
    await panel.evaluate((element) => element.getBoundingClientRect().width),
  ).toBeLessThanOrEqual(448);
  expect(
    await panel.evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ),
  ).not.toBe(
    await page
      .getByRole("main")
      .evaluate((element) => getComputedStyle(element).backgroundColor),
  );
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: info.outputPath("login-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: info.outputPath("login-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("password flow retains CSRF and submitter values before mounting the workspace", async ({
  page,
}) => {
  await signIn(page);
  await page.getByLabel("Account menu").click();
  await expect(
    page.getByRole("link", { name: "Administration", exact: true }),
  ).toBeVisible();
});

test("administration disclosures, spacing and graph selections remain actionable", async ({
  page,
}, info) => {
  await mockDashboard(page);
  await page.goto("/settings/sso#users");
  const advanced = page.getByText("Link an existing account · advanced", {
    exact: true,
  });
  await advanced.click();
  await expect(
    page.getByLabel("Company domain", { exact: true }),
  ).toBeVisible();
  await advanced.click();
  await expect(page.getByLabel("Company domain", { exact: true })).toBeHidden();
  await page.getByRole("link", { name: /Sign-in & security/ }).click();
  const instructions = page.getByRole("button", {
    name: "Get verification instructions",
  });
  const next = page.getByRole("button", { name: "Continue", exact: true });
  const first = await instructions.boundingBox();
  const second = await next.boundingBox();
  expect(
    first &&
      second &&
      (second.y >= first.y + first.height + 8 ||
        second.x >= first.x + first.width + 8),
  ).toBe(true);
  await page.getByRole("link", { name: /Permissions/ }).click();
  await page.getByRole("button", { name: /Manage roles and people/ }).click();
  await page.locator(".vue-flow__node").filter({ hasText: "analyst" }).click();
  await expect(
    page.getByRole("dialog", { name: "Access for analyst" }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Who needs access?" }),
  ).toContainText("analyst");
  await expect(
    page.getByRole("button", { name: "Edit general access rules" }),
  ).toBeVisible();
  await page
    .getByRole("textbox", { name: "Display name", exact: true })
    .fill("Data analysts");
  await page
    .getByRole("button", { name: "Save display name", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Access for Data analysts" }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Who needs access?" }),
  ).toContainText("Data analysts");
  await page.getByRole("dialog").evaluate((element) => (element.scrollTop = 0));
  await page.screenshot({
    path: info.outputPath("permissions-desktop.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Close access editor", exact: true })
    .click();
  await page.locator(".vue-flow__node").filter({ hasText: "Alex" }).click();
  await expect(
    page.getByRole("combobox", { name: "Who needs access?" }),
  ).toContainText("Alex");
  await page
    .getByRole("textbox", { name: "Display name", exact: true })
    .fill("Alex Morgan");
  await page
    .getByRole("button", { name: "Save display name", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Access for Alex Morgan" }),
  ).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: /Manage roles and people/ }).click();
  await expect(
    page.locator(".vue-flow__node").filter({ hasText: "Data analysts" }),
  ).toBeVisible();
  await expect(
    page.locator(".vue-flow__node").filter({ hasText: "Alex Morgan" }),
  ).toBeVisible();
});

test("Pengolo row actions, project editing and tool menus preserve their contracts", async ({
  page,
}) => {
  await mockDashboard(page);
  await page.goto("/pengolo");
  const options = page.getByRole("button", {
    name: "Options for project Operations",
    exact: true,
  });
  await expect(options).toHaveCSS("opacity", "1");
  await expect(options).toHaveCSS("pointer-events", "auto");
  await options.click();
  await page.getByRole("menuitem", { name: "Rename and permissions" }).click();
  await expect(
    page.getByRole("dialog", { name: "Edit project" }),
  ).toBeVisible();
  await expect(
    page.getByRole("textbox", { name: "Project name", exact: true }),
  ).toHaveValue("Operations");
  await page
    .getByRole("textbox", { name: "Project name", exact: true })
    .fill("Research operations");
  await page.getByRole("button", { name: "Save project", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Edit project" })).toBeHidden();
  await expect(page.locator(".p-project-title")).toContainText(
    "Research operations",
  );
  const row = page.locator(".p-chat-row").filter({ hasText: "Daily review" });
  await expect(
    row.getByRole("button", { name: "Daily review", exact: true }),
  ).toBeVisible();
  const remove = page.getByRole("button", {
    name: "Delete Daily review",
    exact: true,
  });
  await page.mouse.move(0, 0);
  await expect(remove).toHaveCSS("opacity", "0");
  await row.hover();
  await expect(remove).toHaveCSS("opacity", "1");
  await page.getByRole("button", { name: "Add files and tools" }).click();
  const specialist = page.getByRole("menuitem", { name: "Specialist agents" });
  await expect(specialist.locator("svg")).toHaveCount(1);
  const attach = page.getByRole("menuitem", {
    name: "Attach files",
    exact: true,
  });
  const first = await specialist.boundingBox();
  const second = await attach.boundingBox();
  expect(first && second && second.y >= first.y + first.height).toBe(true);
  await expect(
    page.getByRole("menuitem", { name: "Galadril Graph Context" }),
  ).toHaveCount(0);
});

test("workspace preserves search hierarchy, navigation alignment and composer layout", async ({
  page,
}, info) => {
  await mockDashboard(page);
  await page.goto("/pengolo");
  const search = page.locator('[data-tour="search"]');
  expect(
    await search.evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ),
  ).not.toBe("rgb(245, 158, 11)");
  await search.click();
  await expect(
    page.getByRole("dialog", { name: "Search Galadril", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("dialog", { name: "Search Galadril", exact: true }),
  ).toBeHidden();
  const nav = page.getByRole("navigation", { name: "Pengolo workspace" });
  await expect(nav.getByRole("link", { name: "Scheduled" })).toBeVisible();
  const icons = await nav
    .locator("svg")
    .evaluateAll((elements) =>
      elements.map((element) => element.getBoundingClientRect().left),
    );
  expect(Math.max(...icons) - Math.min(...icons)).toBeLessThan(2);
  const prompt = page.getByRole("textbox", {
    name: "Message Pengolo",
    exact: true,
  });
  const composer = page.locator(".p-composer");
  const promptBox = await prompt.boundingBox();
  const composerBox = await composer.boundingBox();
  expect(
    promptBox && composerBox && promptBox.width > composerBox.width * 0.8,
  ).toBe(true);
  expect(promptBox && promptBox.height < 70).toBe(true);
  await prompt.fill("Review current operations");
  await expect(
    page.getByRole("button", { name: "Send message", exact: true }),
  ).toBeEnabled();
  await page.screenshot({
    path: info.outputPath("workspace-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: info.outputPath("workspace-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("administration schema mismatches explain the update required instead of suggesting retries", async ({
  page,
}) => {
  await mockDashboard(page);
  await page.route("**/graphql", (route) =>
    route.request().postDataJSON().query.includes("accessGraph")
      ? route.fulfill({
          status: 400,
          json: {
            errors: [
              {
                message: 'Unknown field "displayName" on type "Role"',
                locations: [{ line: 1, column: 307 }],
              },
            ],
          },
        })
      : route.fallback(),
  );
  await page.goto("/settings/sso#permissions");
  await page.getByRole("button", { name: /Manage roles and people/ }).click();
  await expect(
    page
      .getByRole("alert")
      .filter({
        hasText:
          "This page is incompatible with the administration service. Ask your administrator to complete the update, then reload this page.",
      })
      .first(),
  ).toBeVisible();
});

test("library explains an extraction credential failure and keeps the document available", async ({
  page,
}, info) => {
  await mockDashboard(page, [
    {
      id: "file-credential-fixture",
      ownerId: "ui-user",
      projectId: null,
      revision: 1,
      expiresAt: Date.now() + 86400000,
      data: {
        name: "Exam charter.pdf",
        status: "ready",
        mime: "application/pdf",
        size: 194560,
        indexing: { stage: "failed", percent: 95 },
        retrieval: {
          status: "ready",
          warnings: [
            "graph_extraction_unavailable",
            "graph_extraction_unauthorized",
          ],
        },
      },
    },
  ]);
  await page.goto("/pengolo");
  await page.getByRole("link", { name: "Library", exact: true }).click();
  const library = page.getByRole("region", {
    name: "File library",
    exact: true,
  });
  await expect(
    library.getByText("Exam charter.pdf", { exact: true }),
  ).toBeVisible();
  await expect(
    library.getByText(
      "The extraction service rejected its credentials. The document remains available, but some search features are incomplete. Ask your administrator to update the credentials, then retry indexing.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Publishing search index", { exact: false }),
  ).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath("indexing-credentials-desktop.png"),
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Collapse Pengolo sidebar", exact: true })
    .click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    library.getByText("Exam charter.pdf", { exact: true }),
  ).toBeInViewport();
  await page.screenshot({
    path: info.outputPath("indexing-credentials-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});
