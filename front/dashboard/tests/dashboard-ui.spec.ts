import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { parse, Kind } from "graphql";

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (error) => console.error(error.stack));
  page.on("console", (message) => {
    if (message.type() === "error") console.error(message.text());
  });
});

async function mockDashboard(
  page: Page,
  libraryItems: object[] = [],
  overrides: Record<string, unknown> = {},
) {
  const shell = await page.request.get("/login");
  for (const path of ["/pengolo", "/pengolo?*", "/pengolo/*", "/settings/sso"])
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
      const request = JSON.parse(String(raw));
      if (request.type === "connection_init")
        socket.send(JSON.stringify({ type: "connection_ack" }));
      if (
        request.type === "subscribe" &&
        request.payload.query.includes("pengoloStream") &&
        overrides.pengoloStream
      )
        socket.send(
          JSON.stringify({
            id: request.id,
            type: "next",
            payload: { data: { pengoloStream: overrides.pengoloStream } },
          }),
        );
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
              name === "activeChat" && !payload.variables?.withChat
                ? null
                : (overrides[name] ?? response.json.data[name] ?? null),
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
  const specialist = page.getByRole("menuitem", { name: "Specialist teams" });
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
  const promptBox = await page.locator(".p-composer-entry").boundingBox();
  const composerBox = await composer.boundingBox();
  expect(
    promptBox && composerBox && promptBox.width > composerBox.width * 0.5,
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

const chatFixtureId = "a".repeat(32);
function chatFixture(modelAlias = "small", generation: string | null = null) {
  return {
    id: chatFixtureId,
    ownerId: "ui-user",
    projectId: null,
    revision: 1,
    updatedAt: 1,
    data: {
      title: "Saved conversation",
      pinned: false,
      activeGeneration: generation,
      generationContext: {
        modelAlias,
        graphContext: null,
        webSearch: false,
        agentIds: [],
      },
      messages: {
        items: [
          {
            id: "message-fixture",
            role: "assistant",
            text: "Choose your answer below.",
            status: "completed",
            evidence: [],
          },
        ],
        after: null,
      },
    },
  };
}
const questionFixture = {
  type: "question",
  id: "question-fixture",
  questions: [
    {
      header: "Sources",
      question: "Quel tableau souhaitez-vous ?",
      multi_select: false,
      options: [
        {
          label: "Comparatif des options",
          description: "Comparer les avantages et les contraintes.",
        },
        {
          label: "Chronologie détaillée",
          description: "Présenter les dates et les étapes.",
        },
      ],
    },
  ],
};

test("Pengolo interleaves collapsed checks with a readable timeline and risk table", async ({
  page,
}, info) => {
  const chat = chatFixture("fixture");
  const intro = "What the document establishes.\n\n";
  const fence = (kind: string, body: unknown) =>
    "\x60\x60\x60" + kind + "\n" + JSON.stringify(body) + "\n\x60\x60\x60\n\n";
  const message = {
    ...chat.data.messages.items[0],
    text:
      intro +
      fence("timeline", {
        title: "Sample project chronology",
        items: [
          {
            label: "10 March",
            detail: "Delivery recorded in the sample report",
            source: "Sample report, p. 3",
          },
        ],
      }) +
      fence("risk", {
        title: "Provisional assessment",
        items: [
          {
            label: "Handover",
            detail: "Sign-off remains unconfirmed",
            level: "serious",
          },
        ],
      }) +
      fence("reasoning", {
        title: "From rule to consequence",
        items: [{ label: "Rule", detail: "Check the applicable version" }],
        outcomes: [
          { label: "If verified", detail: "Assess the effect" },
          { label: "If disputed", detail: "Obtain the original exhibit" },
        ],
      }) +
      "![Tracking](https://tracker.invalid/private-value)",
    toolCheckpoints: [{ offset: intro.length, operation: "web" }],
  };
  await mockDashboard(page, [], {
    activeChat: {
      ...chat,
      data: { ...chat.data, messages: { items: [message], after: null } },
    },
    pengoloChat: {
      ...chat,
      data: { ...chat.data, messages: { items: [message], after: null } },
    },
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const response = page.getByRole("article", { name: "Pengolo response" });
  const checks = response.locator("details.p-thinking");
  await expect(checks.locator("summary")).toHaveCSS("list-style-type", "none");
  await expect(checks).not.toHaveAttribute("open");
  await expect(response.getByText("Web search", { exact: true })).toBeHidden();
  await checks.getByText("Reasoning", { exact: true }).click();
  await expect(checks.getByText("Web search", { exact: true })).toBeVisible();
  await expect(response.getByRole("table")).toContainText("Serious");
  await expect(response.getByText("Sample report, p. 3")).toBeVisible();
  await expect(
    response.getByRole("list", { name: "Possible consequences" }),
  ).toContainText("If disputed");
  await expect(response.locator("img")).toHaveCount(0);
  await checks.getByText("Reasoning", { exact: true }).click();
  await response.screenshot({
    path: info.outputPath("progressive-response-desktop.png"),
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(response.getByRole("table")).toBeVisible();
  await response.screenshot({
    path: info.outputPath("progressive-response-mobile.png"),
    animations: "disabled",
  });
});

test("Pengolo keeps removable web search inside the prompt row", async ({
  page,
}, info) => {
  await mockDashboard(page);
  await page.goto("/pengolo");
  const prompt = page.getByRole("textbox", {
    name: "Message Pengolo",
    exact: true,
  });
  const enable = async () => {
    await page.getByRole("button", { name: "Add files and tools" }).click();
    await page.getByRole("menuitem", { name: "Web search" }).click();
  };
  await enable();
  const remove = page.getByRole("button", {
    name: "Disable web search",
    exact: true,
  });
  const row = page.locator(".p-composer-input");
  await expect(row.getByRole("textbox")).toBeVisible();
  await expect(
    row.getByRole("button", { name: "Disable web search" }),
  ).toBeVisible();
  await expect(remove.locator(".p-tool-remove")).toHaveCSS("opacity", "0");
  await remove.hover();
  await expect(remove.locator(".p-tool-remove")).toHaveCSS("opacity", "1");
  await prompt.fill("Compare public sources");
  await prompt.press("ControlOrMeta+A");
  await prompt.press("ArrowLeft");
  await prompt.press("Backspace");
  await expect(remove).toHaveCount(0);
  await expect(prompt).toHaveText("Compare public sources");
  await enable();
  await prompt.press("ControlOrMeta+A");
  await prompt.press("ArrowLeft");
  await prompt.press("Delete");
  await expect(remove).toHaveCount(0);
  await expect(prompt).toHaveText("Compare public sources");
  await enable();
  await page.mouse.move(0, 0);
  await page.locator(".p-composer").screenshot({
    path: info.outputPath("inline-web-desktop.png"),
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(remove).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.locator(".p-composer").screenshot({
    path: info.outputPath("inline-web-mobile.png"),
    animations: "disabled",
  });
  await remove.click();
  await expect(remove).toHaveCount(0);
  await expect(
    page.getByText("Connected to your Galadril workspace", { exact: true }),
  ).toHaveCount(0);
});

test("Pengolo aligns short prompts with a combined model menu and expands long prompts", async ({
  page,
}, info) => {
  await mockDashboard(page, [], {
    pengoloModels: {
      defaultModel: "gpt-6.1-sol",
      models: [
        {
          alias: "gpt-6.1-sol",
          model: "gpt-6.1-sol",
          provider: "openai",
          reasoningEfforts: ["medium"],
        },
        {
          alias: "gpt-6-astra",
          model: "gpt-6-astra",
          provider: "openai",
          reasoningEfforts: ["medium"],
        },
      ],
    },
  });
  await page.goto("/pengolo");
  const prompt = page.getByRole("textbox", {
    name: "Message Pengolo",
    exact: true,
  });
  const model = page.getByRole("button", { name: "Model", exact: true });
  await prompt.fill("Compare public sources");
  await expect(model).toBeVisible();
  await expect(prompt).toHaveCSS("font-size", "14px");
  await expect(model).toHaveCSS("font-size", "14px");
  const promptBox = await prompt.boundingBox();
  const modelBox = await model.boundingBox();
  expect(promptBox && modelBox).toBeTruthy();
  expect(
    Math.abs(
      promptBox!.y +
        promptBox!.height / 2 -
        (modelBox!.y + modelBox!.height / 2),
    ),
  ).toBeLessThan(3);
  await expect(
    page.locator(".p-composer").getByText("Medium", { exact: true }),
  ).toHaveCount(0);
  await model.click();
  await expect(
    page.getByRole("menuitem", { name: "Deep investigation", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("menuitem", { name: /Thinking effort/ }),
  ).toBeDisabled();
  await expect(
    page.getByRole("menuitem", { name: /Thinking effort/ }),
  ).toContainText("Medium");
  await page.getByRole("menu").screenshot({
    path: info.outputPath("model-menu-desktop.png"),
    animations: "disabled",
  });
  await page
    .getByRole("menuitem", { name: "GPT-6 Astra", exact: true })
    .click();
  await expect(model).toContainText("GPT-6 Astra");
  await page.locator(".p-composer").screenshot({
    path: info.outputPath("single-row-composer.png"),
    animations: "disabled",
  });
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Web search" }).click();
  await prompt.fill(
    "Compare the available public sources and explain the uncertain points. ".repeat(
      20,
    ),
  );
  await expect(page.locator(".p-composer")).toHaveClass(/expanded/);
  const rowWidth = (await page.locator(".p-composer-input").boundingBox())!
    .width;
  await prompt.evaluate((element) => {
    element.scrollTop = 0;
    element.dispatchEvent(new Event("scroll"));
  });
  expect((await prompt.boundingBox())!.width).toBeGreaterThan(rowWidth * 0.9);
  const lines = await prompt.evaluate((element) =>
    Array.from(element.getClientRects(), (rect) => ({
      x: rect.x,
      width: rect.width,
    })),
  );
  const toolLine = await page.locator(".p-tool-label").evaluate((element) => {
    const text = Array.from(element.childNodes).find(
      (node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim(),
    );
    if (!text) throw new Error("Missing tool label");
    const range = document.createRange();
    range.selectNode(text);
    return range.getBoundingClientRect().y;
  });
  const promptLine = await prompt.evaluate((element) => {
    const range = document.createRange();
    range.selectNodeContents(element);
    return range.getClientRects()[0]?.y;
  });
  expect(promptLine).toBeDefined();
  expect(Math.abs(toolLine - promptLine!)).toBeLessThan(2);
  expect(lines.length).toBeGreaterThan(1);
  expect(lines[1]!.x).toBeLessThan(lines[0]!.x - 80);
  await expect
    .poll(async () => (await prompt.boundingBox())?.height ?? 0)
    .toBeGreaterThan(100);
  expect((await model.boundingBox())?.y ?? 0).toBeGreaterThan(
    (await prompt.boundingBox())?.y ?? Infinity,
  );
  await page.locator(".p-composer-entry").evaluate((element) => {
    element.scrollTop = 0;
  });
  await page.locator(".p-composer").screenshot({
    path: info.outputPath("expanded-composer-desktop.png"),
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.locator(".p-composer").screenshot({
    path: info.outputPath("expanded-composer-mobile.png"),
    animations: "disabled",
  });
});

test("Pengolo attaches a library document by clicking its compact row", async ({
  page,
}, info) => {
  const file = {
    id: "d".repeat(32),
    ownerId: "ui-user",
    projectId: null,
    revision: 1,
    data: {
      name: "Public report.pdf",
      mime: "application/pdf",
      size: 2048,
      status: "ready",
      indexing: { stage: "completed" },
    },
  };
  await mockDashboard(page, [file], {
    pengoloDownload: {
      name: file.data.name,
      mime: file.data.mime,
      size: file.data.size,
      url: "/fixture-file",
    },
  });
  await page.route("**/fixture-file*", (route) =>
    route.fulfill({
      contentType: "application/pdf",
      body: Buffer.from(
        "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoKPDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUgL1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCAzMDAgMTgwXSAvUmVzb3VyY2VzIDw8IC9Gb250IDw8IC9GMSA1IDAgUiA+PiA+PiAvQ29udGVudHMgNCAwIFIgPj4KZW5kb2JqCjQgMCBvYmoKPDwgL0xlbmd0aCA1NSA+PgpzdHJlYW0KQlQgL0YxIDE2IFRmIDM1IDEwMCBUZCAoUHVibGljIHJlcG9ydCAtIHByZXZpZXcpIFRqIEVUCmVuZHN0cmVhbQplbmRvYmoKNSAwIG9iago8PCAvVHlwZSAvRm9udCAvU3VidHlwZSAvVHlwZTEgL0Jhc2VGb250IC9IZWx2ZXRpY2EgPj4KZW5kb2JqCnhyZWYKMCA2CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAwOSAwMDAwMCBuIAowMDAwMDAwMDU4IDAwMDAwIG4gCjAwMDAwMDAxMTUgMDAwMDAgbiAKMDAwMDAwMDI0MSAwMDAwMCBuIAowMDAwMDAwMzQ1IDAwMDAwIG4gCnRyYWlsZXIKPDwgL1NpemUgNiAvUm9vdCAxIDAgUiA+PgpzdGFydHhyZWYKNDE1CiUlRU9GCg==",
        "base64",
      ),
    }),
  );
  await page.goto("/pengolo");
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Choose from library" }).click();
  const dialog = page.getByRole("dialog", { name: "Attach from library" });
  await expect(dialog.getByText("Search options", { exact: true })).toHaveCount(
    0,
  );
  await expect(dialog.getByText(/Your personal library/)).toHaveCount(0);
  await expect(
    dialog.getByText("Document indexed", { exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Attach", exact: true }),
  ).toHaveCount(0);
  await dialog.screenshot({
    path: info.outputPath("compact-library-desktop.png"),
    animations: "disabled",
  });
  await dialog
    .getByRole("button", { name: "Public report.pdf", exact: true })
    .click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".p-composer .p-file-chips")).toContainText(
    "Public report.pdf",
  );
  const chip = page
    .locator(".p-composer .p-file-chips")
    .getByRole("button", { name: "Public report.pdf", exact: true });
  await chip.click();
  const preview = page.getByRole("dialog", {
    name: "Public report.pdf",
    exact: true,
  });
  await expect(preview).toBeVisible();
  await expect(preview.locator("iframe")).toBeVisible();
  await preview.screenshot({
    path: info.outputPath("selected-file-preview.png"),
    animations: "disabled",
  });
  await preview
    .getByRole("button", { name: "Close dialog", exact: true })
    .click();
  await expect(chip).toBeVisible();
});

test("Pengolo folds live tasks into Thinking without hiding questions", async ({
  page,
}) => {
  const task = {
    type: "plan",
    operation: "upsert",
    task: {
      id: "check",
      content: "Check public sources",
      active_form: "Checking public sources",
      status: "in_progress",
    },
  };
  const chat = chatFixture("fixture", "run-fixture");
  const runningChat = {
    ...chat,
    data: {
      ...chat.data,
      messages: {
        ...chat.data.messages,
        items: chat.data.messages.items.map((message) => ({
          ...message,
          toolCheckpoints: [{ offset: 0, operation: "web" }],
        })),
      },
    },
  };
  await mockDashboard(page, [], {
    activeChat: runningChat,
    pengoloChat: runningChat,
    pengoloStream: {
      generationId: "run-fixture",
      status: "running",
      canAnswer: true,
      events: [
        { sequence: 1, kind: "tool", content: "web" },
        { sequence: 2, kind: "activity", content: JSON.stringify(task) },
        {
          sequence: 3,
          kind: "activity",
          content: JSON.stringify(questionFixture),
        },
      ],
    },
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const web = page.getByRole("button", {
    name: "Disable web search",
    exact: true,
  });
  await expect(web).toBeDisabled();
  await web.hover({ force: true });
  await expect(web.locator(".p-tool-globe")).toHaveCSS("opacity", "1");
  await expect(web.locator(".p-tool-remove")).toHaveCSS("opacity", "0");
  const thinking = page.locator(".p-agent-activity details.p-thinking");
  await expect(thinking).not.toHaveAttribute("open");
  await expect(
    page.getByText("Checking public sources", { exact: true }),
  ).toBeHidden();
  await expect(
    page.getByRole("region", {
      name: "Question question-fixture",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page
      .locator(".p-message.assistant")
      .last()
      .getByText("Reasoning", { exact: true }),
  ).toHaveCount(0);
  const heading = thinking.locator("summary").first();
  const dots = await heading.locator(".p-agent-typing").boundingBox();
  const label = await heading.locator("span").last().boundingBox();
  expect(
    Math.abs(dots!.y + dots!.height / 2 - label!.y - label!.height / 2),
  ).toBeLessThan(2);
  await thinking.getByText("Thinking", { exact: true }).click();
  await expect(
    thinking.getByText("Checking public sources", { exact: true }),
  ).toBeVisible();
});

test("Pengolo selects a plugin team and explains its specialists without selecting them individually", async ({
  page,
}, info) => {
  const roles = [
    {
      pluginId: "law",
      name: "Facts and exhibits",
      skill: "law.skill.faits",
      mission: "Build a chronology from the available exhibits.",
    },
    {
      pluginId: "law",
      name: "Case law research",
      skill: "law.skill.jurisprudence",
      mission: "Find and verify relevant public decisions.",
    },
    {
      pluginId: "utils",
      name: "Email correspondence",
      skill: "utils.skill.email",
      mission: "Summarize conversations and prepare replies for review.",
    },
  ];
  await mockDashboard(page, [], {
    pengoloProjects: [
      {
        id: "legal-project",
        ownerId: "ui-user",
        revision: 1,
        data: {
          name: "Case",
          icon: "folder",
          color: "#d97706",
          shared: false,
          roles: [],
          contextMode: "project",
          executionContext: "law.administratif",
        },
      },
    ],
    pengoloAgents: roles.map((role, index) => ({
      id: String(index).repeat(32),
      ownerId: "",
      projectId: null,
      revision: 1,
      data: { ...role, skillIds: [role.skill], readOnly: true },
    })),
    scribeCapabilities: {
      capabilities: roles.map((role) => ({
        id: role.skill,
        pluginId: role.pluginId,
        kind: "SKILL",
        execution: "REGISTERED",
        name: role.name,
        description: role.mission,
        executionProfile: {
          requiresContext: role.pluginId === "law" ? "law.administratif" : null,
          isolation: "standard",
          contexts:
            role.pluginId === "law"
              ? [
                  {
                    id: "law.administratif",
                    name: "Contentieux administratif",
                    aliases: ["administratif"],
                    privacy: "pseudonymized",
                  },
                ]
              : [],
        },
        compatibility: { allowedModels: [], requires: [] },
      })),
    },
  });
  await page.goto("/pengolo");
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Specialist teams" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Specialist teams",
    exact: true,
  });
  await expect(
    dialog.getByRole("button", {
      name: "Your custom specialists",
      exact: true,
    }),
  ).toHaveCount(0);
  const law = dialog.getByRole("region", {
    name: "Law — Contentieux administratif",
    exact: true,
  });
  await expect(law.locator("button svg").first()).toHaveCSS(
    "color",
    "rgb(4, 120, 87)",
  );
  await expect(law.getByRole("checkbox")).toHaveCount(0);
  const choose = law.getByRole("button", {
    name: "Law — Contentieux administratif",
    exact: true,
  });
  await expect(choose).toBeDisabled();
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await page.goto("/pengolo?project=legal-project");
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Specialist teams" }).click();
  await expect(choose).toBeEnabled();
  await choose.click();
  await expect(choose).toHaveAttribute("aria-pressed", "true");
  await choose.hover();
  await expect(choose).toHaveCSS(
    "background-color",
    await law.evaluate((element) => getComputedStyle(element).backgroundColor),
  );
  await choose.click();
  await expect(choose).toHaveAttribute("aria-pressed", "false");
  await choose.focus();
  await page.keyboard.press("Space");
  await expect(choose).toHaveAttribute("aria-pressed", "true");
  await expect(
    law.getByRole("button", { name: /Use this team|Remove team/ }),
  ).toHaveCount(0);
  await expect(
    law.getByText("Facts and exhibits", { exact: true }),
  ).toBeHidden();
  await law.getByText("View 2 specialists", { exact: true }).click();
  await expect(
    law.getByText("Facts and exhibits", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("Email & correspondence", { exact: true }),
  ).toHaveCount(0);
  await dialog.screenshot({
    path: info.outputPath("specialist-teams-desktop.png"),
    animations: "disabled",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog.screenshot({
    path: info.outputPath("specialist-teams-mobile.png"),
    animations: "disabled",
  });
  await dialog.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.locator(".p-composer")).toContainText(
    "Law — Contentieux administratif",
  );
});

test("Pengolo keeps a saved chat readable when its teams cannot be restored", async ({
  page,
}) => {
  const chat = chatFixture("fixture");
  const saved = {
    ...chat,
    data: {
      ...chat.data,
      generationContext: {
        ...chat.data.generationContext,
        agentIds: ["old-team"],
      },
    },
  };
  await mockDashboard(page, [], { activeChat: saved, pengoloChat: saved });
  await page.route("**/graphql", (route) =>
    route.request().postDataJSON().query.includes("query Agents")
      ? route.fulfill({ json: { errors: [{ message: "Unavailable" }] } })
      : route.fallback(),
  );
  await page.goto("/pengolo/" + chatFixtureId);
  await expect(
    page.getByRole("article", { name: "Pengolo response" }),
  ).toContainText("Choose your answer below.");
  await expect(page.getByRole("alert")).toContainText(
    "Your teams could not be restored",
  );
  await expect(
    page.getByRole("textbox", { name: "Message Pengolo", exact: true }),
  ).toBeEnabled();
});

test("Pengolo keeps the project and conversation mode visible and returns to the latest response", async ({
  page,
  context,
}, info) => {
  const chat = chatFixture("fixture");
  const projectId = "c".repeat(32);
  const projectChat = { ...chat, projectId };
  chat.data.messages.items = Array.from({ length: 10 }, (_, index) => ({
    id: `response-${index}`,
    role: "assistant",
    status: "completed",
    evidence: [],
    text: `Response ${index}\n\n${"Verified evidence. ".repeat(60)}`,
  }));
  await mockDashboard(page, [], {
    activeChat: projectChat,
    pengoloChat: projectChat,
    pengoloProjects: [
      {
        id: projectId,
        ownerId: "ui-user",
        projectId: null,
        revision: 1,
        updatedAt: 1,
        data: {
          name: "Administrative litigation",
          icon: "folder",
          color: "#92400e",
        },
      },
    ],
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const header = page.locator(".p-header");
  await expect(header).toContainText("Administrative litigation");
  await expect(header.getByRole("heading")).toHaveText("Saved conversation");
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const latest = page.getByRole("article").last();
  await latest.hover();
  await latest.getByRole("button", { name: "Copy response" }).click();
  await expect(
    latest.getByRole("button", { name: "Copied", exact: true }),
  ).toBeVisible();
  await expect(
    latest.getByRole("button", { name: "Copy response" }),
  ).toBeVisible({
    timeout: 950,
  });
  await page.locator(".p-conversation").evaluate((element) => {
    element.scrollTop = 0;
  });
  await page.getByRole("button", { name: "Back to latest message" }).click();
  await expect(latest).toBeInViewport();
  await expect(
    page.getByRole("button", { name: "Back to latest message" }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page
        .locator(".p-sidebar")
        .evaluate((element) => element.getBoundingClientRect().right),
    )
    .toBeLessThanOrEqual(0);
  await expect(header.getByText("Administrative litigation")).toBeVisible();
  await header.getByRole("button", { name: "Saved chat" }).click();
  await expect(page.getByRole("menu")).toBeVisible();
  await page.screenshot({ path: info.outputPath("chat-header-mobile.png") });
  await page.getByRole("menuitem", { name: /^Temporary with library/ }).click();
  await expect(
    header.getByRole("button", { name: "Temporary with library" }),
  ).toBeVisible();
  await expect(page.locator(".p-temporary-banner")).toContainText(
    "Your personal library is available",
  );
});

test("saved conversations restore their own model after navigation and reload", async ({
  page,
}) => {
  await mockDashboard(page, [], {
    activeChat: chatFixture(),
    pengoloChat: chatFixture(),
    pengoloChats: {
      items: [
        {
          ...chatFixture(),
          data: { title: "Saved conversation", pinned: false },
        },
      ],
      after: null,
    },
    pengoloModels: {
      defaultModel: "large",
      models: [
        {
          alias: "large",
          model: "Large model",
          provider: "fixture",
          reasoningEfforts: ["medium"],
        },
        {
          alias: "small",
          model: "Small model",
          provider: "fixture",
          reasoningEfforts: ["medium"],
        },
      ],
    },
  });
  await page.goto("/pengolo");
  await page
    .getByRole("button", { name: "Saved conversation", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Model", exact: true }),
  ).toContainText("Small model");
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Model", exact: true }),
  ).toContainText("Small model");
});

test("ask_user accepts typed and selected answers without losing a draft on failure", async ({
  page,
}, info) => {
  await mockDashboard(page, [], {
    activeChat: chatFixture("fixture", "run-fixture"),
    pengoloChat: chatFixture("fixture", "run-fixture"),
    pengoloStream: {
      generationId: "run-fixture",
      status: "running",
      canAnswer: true,
      events: [
        {
          sequence: 1,
          kind: "activity",
          content: JSON.stringify(questionFixture),
        },
      ],
    },
  });
  let answer: Record<string, unknown> | null = null;
  await page.route("**/graphql", async (route) => {
    const payload = route.request().postDataJSON();
    if (payload.query.includes("answerPengoloQuestion")) {
      answer = payload.variables.input;
      return route.fulfill({ json: { errors: [{ message: "retry" }] } });
    }
    return route.fallback();
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const field = page.getByRole("textbox", {
    name: "Your answer for Sources",
    exact: true,
  });
  await page.getByText("Write my own answer", { exact: true }).click();
  await expect(field).toBeVisible();
  await field.fill("Un tableau des recours et des délais");
  await expect(field).toHaveValue("Un tableau des recours et des délais");
  const question = page.getByRole("region", {
    name: "Question question-fixture",
    exact: true,
  });
  await expect(
    page
      .getByRole("article", { name: "Pengolo response" })
      .last()
      .getByRole("region", {
        name: "Question question-fixture",
        exact: true,
      }),
  ).toBeVisible();
  const radio = question.getByRole("radio").first();
  const bounds = await radio.boundingBox();
  expect(bounds?.width).toBeLessThan(24);
  await page.getByRole("button", { name: "Send answer", exact: true }).click();
  await expect(question.getByRole("alert")).toBeVisible();
  await expect(field).toHaveValue("Un tableau des recours et des délais");
  expect(answer).toMatchObject({
    answers: [
      {
        header: "Sources",
        customAnswer: "Un tableau des recours et des délais",
        selected: [],
      },
    ],
  });
  await radio.check();
  await expect(field).toHaveValue("");
  await page.screenshot({
    path: info.outputPath("ask-user-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole("button", { name: "Send answer", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    page.getByRole("button", { name: "Send answer", exact: true }),
  ).toBeInViewport();
  expect(
    (await new AxeBuilder({ page }).include(".p-agent-question").analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: info.outputPath("ask-user-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("completed questions remain inside the response and outside copied text", async ({
  page,
  context,
}) => {
  const runId = "run-fixture";
  const responseText = "Here is the completed legal analysis.";
  const response = {
    id: "finished-response",
    role: "assistant",
    text: responseText,
    status: "completed",
    generationId: runId,
    activityKinds: ["question"],
    evidence: [],
  };
  const events = [
    { sequence: 1, kind: "activity", content: JSON.stringify(questionFixture) },
    {
      sequence: 2,
      kind: "activity",
      content: JSON.stringify({
        type: "question_resolved",
        id: questionFixture.id,
        cancelled: false,
      }),
    },
    { sequence: 3, kind: "completed", content: "" },
  ];
  const replay = {
    generationId: runId,
    status: "completed",
    canAnswer: false,
    message: response,
    events,
  };
  const running = chatFixture("fixture", runId);
  await mockDashboard(page, [], {
    activeChat: running,
    pengoloChat: running,
    pengoloStream: replay,
  });
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/pengolo/" + chatFixtureId);
  const article = page
    .getByRole("article", { name: "Pengolo response" })
    .filter({ hasText: responseText });
  const question = article.getByRole("region", {
    name: "Question question-fixture",
  });
  const copy = article.getByRole("button", {
    name: "Copy response",
    exact: true,
  });
  await expect(question).toContainText("Quel tableau souhaitez-vous ?");
  expect((await question.boundingBox())?.y ?? Infinity).toBeLessThan(
    (await copy.boundingBox())?.y ?? 0,
  );
  await article.hover();
  await copy.click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    responseText,
  );

  const saved = chatFixture("fixture");
  saved.data.messages.items = [response];
  await mockDashboard(page, [], {
    activeChat: saved,
    pengoloChat: saved,
    pengoloEvents: replay,
  });
  await page.reload();
  await expect(question).toContainText("Quel tableau souhaitez-vous ?");
  await expect(copy).toBeVisible();
});

test("Pengolo sends two files only after the user approves the email", async ({
  page,
}) => {
  const id = "d".repeat(32);
  const proposal = {
    type: "email_draft",
    id,
    to: ["recipient@example.org"],
    cc: [],
    bcc: [],
    subject: "Documents for review",
    body: "Please see the attached files.",
  };
  const chat = chatFixture("fixture", "run-fixture");
  await mockDashboard(page, [], {
    activeChat: chat,
    pengoloChat: chat,
    pengoloStream: {
      generationId: "run-fixture",
      status: "running",
      canAnswer: false,
      events: [
        { sequence: 1, kind: "activity", content: JSON.stringify(proposal) },
      ],
    },
  });
  const mutations: string[] = [];
  await page.route("**/graphql", async (route) => {
    const payload = route.request().postDataJSON();
    if (!payload.query.includes("mutation Email")) return route.fallback();
    const request = JSON.parse(payload.variables.request);
    const respond = (emailOperation: unknown) =>
      route.fulfill({ json: { data: { emailOperation } } });
    if (request.operation === "proposal_status")
      return respond({ status: "new" });
    if (request.operation === "tracking_status") return respond({});
    if (request.operation === "accounts") {
      return respond([
        {
          id: "e".repeat(32),
          revision: 1,
          rights: ["use", "read", "approve", "send"],
          has_credentials: true,
          sync_status: "ok",
          last_sync: 0,
          config: {
            name: "Test mail",
            address: "sender@example.org",
            imap_port: 993,
            smtp_host: "smtp.example.org",
            smtp_port: 465,
            folders: ["INBOX"],
            enabled: true,
            ingest: false,
            poll_seconds: 300,
          },
        },
      ]);
    }
    if (request.operation === "draft")
      return route.fulfill({ json: { errors: [{ message: "Not found" }] } });
    mutations.push(request.operation);
    if (request.operation === "save_draft") {
      expect(request.content.attachments).toEqual([
        { name: "memoire.txt", content_type: "text/plain", content: "T25l" },
        { name: "bordereau.txt", content_type: "text/plain", content: "VHdv" },
      ]);
      return respond({ revision: 1 });
    }
    return respond({});
  });
  await page.goto("/pengolo/" + chatFixtureId);
  await expect(
    page.locator(".p-thinking > summary .p-agent-typing i"),
  ).toHaveCount(3);
  const attachments = page.getByLabel("Attachments", { exact: true });
  await attachments.setInputFiles([
    { name: "memoire.txt", mimeType: "text/plain", buffer: Buffer.from("One") },
    {
      name: "bordereau.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("Two"),
    },
  ]);
  await expect(page.getByText("bordereau.txt", { exact: true })).toBeVisible();
  expect(mutations).toEqual([]);
  await page.getByRole("button", { name: "Send email", exact: true }).click();
  await expect.poll(() => mutations).toEqual(["save_draft", "approve", "send"]);
});

test("library discovery attaches only the document cited in the final response", async ({
  page,
}) => {
  const ids = ["b".repeat(32), "c".repeat(32), "d".repeat(32)];
  const names = ["chat-photo.png", "other-photo.png", "exam-charter.pdf"];
  const chat = chatFixture("fixture");
  const response = {
    id: "message-fixture",
    role: "assistant",
    text: "La photo est **chat-photo.png**.",
    status: "completed",
    evidence: ids.map((resourceId) => ({
      resourceType: "pengolo_file",
      resourceId,
    })),
  };
  await mockDashboard(page, [], {
    activeChat: {
      ...chat,
      data: { ...chat.data, messages: { items: [response], after: null } },
    },
  });
  await page.route("**/graphql", async (route) => {
    const payload = route.request().postDataJSON();
    if (!payload.query.includes("pengoloDownload")) return route.fallback();
    const index = ids.indexOf(payload.variables.input.id);
    return route.fulfill({
      json: {
        data: {
          pengoloDownload: {
            name: names[index],
            url: "/fixture-file",
            mime: "image/png",
            size: 10,
          },
        },
      },
    });
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const evidence = page.getByLabel("Evidence references", { exact: true });
  await expect(
    evidence.getByRole("button", { name: "chat-photo.png", exact: true }),
  ).toBeVisible();
  await expect(evidence.getByRole("button")).toHaveCount(1);
});

for (const used of [true, false]) {
  test(`Pengolo remembers actual web search usage locally: ${used}`, async ({
    page,
  }) => {
    const chat = chatFixture("fixture");
    const message = {
      ...chat.data.messages.items[0]!,
      toolCheckpoints: used ? [{ offset: 0, operation: "web" }] : [],
    };
    const record = {
      ...chat,
      data: { ...chat.data, messages: { items: [message], after: null } },
    };
    await mockDashboard(page, [], {
      activeChat: record,
      pengoloChat: record,
      pengoloChats: { items: [record], after: null },
    });
    await page.goto("/pengolo/" + chatFixtureId);
    const web = page.getByRole("button", {
      name: "Disable web search",
      exact: true,
    });
    await expect(web).toHaveCount(used ? 1 : 0);
    await expect
      .poll(() =>
        page.evaluate(() =>
          Object.keys(localStorage)
            .filter((k) => k.startsWith("pengolo:web:"))
            .some((k) => localStorage.getItem(k) === "true"),
        ),
      )
      .toBe(used);
    if (!used) {
      await page.getByRole("button", { name: "Add files and tools" }).click();
      await page.getByRole("menuitem", { name: "Web search" }).click();
      await expect(web).toHaveCount(1);
      expect(
        await page.evaluate(() =>
          Object.keys(localStorage)
            .filter((key) => key.startsWith("pengolo:web:"))
            .some((key) => localStorage.getItem(key) === "true"),
        ),
      ).toBe(false);
    }
    if (used) {
      await web.click();
      await expect(web).toHaveCount(0);
    }
    await page.reload();
    await expect(web).toHaveCount(used ? 1 : 0);
  });
}

test("message actions acknowledge copy and export and reveal copying on hover", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const chat = chatFixture("fixture");
  const user = {
    id: "user-copy",
    role: "user",
    text: "A public question.",
    status: "completed",
  };
  const answer = {
    id: "answer-copy",
    role: "assistant",
    text: "| Item | Value |\n| --- | --- |\n| Example | 42 |\n\n```text\nPublic snippet\n```",
    status: "completed",
  };
  const record = {
    ...chat,
    data: { ...chat.data, messages: { items: [user, answer], after: null } },
  };
  await mockDashboard(page, [], {
    activeChat: record,
    pengoloChat: record,
    pengoloChats: { items: [record], after: null },
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const response = page.getByRole("article", {
    name: "Pengolo response",
    exact: true,
  });
  const mine = page.getByRole("article", { name: "Your message", exact: true });
  await expect(response.locator(".p-message-copy")).toHaveCSS("opacity", "0");
  await expect(mine.locator(".p-message-copy")).toHaveCSS("opacity", "0");
  await mine.hover();
  const copy = mine.getByRole("button", {
    name: "Copy your message",
    exact: true,
  });
  await expect(copy).toHaveCSS("opacity", "1");
  await expect(mine.locator(".p-message-body .p-message-copy")).toHaveCount(0);
  const bubbleBounds = await mine.locator(".p-message-body").boundingBox();
  const copyBounds = await copy.boundingBox();
  expect(bubbleBounds).not.toBeNull();
  expect(copyBounds).not.toBeNull();
  expect(copyBounds!.y).toBeGreaterThanOrEqual(
    bubbleBounds!.y + bubbleBounds!.height,
  );
  await copy.click();
  await expect(
    mine.getByRole("button", { name: "Copied", exact: true }),
  ).toBeVisible();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(
    user.text,
  );
  await expect(copy).toHaveCSS("opacity", "1");
  await response.hover();
  for (const label of ["Copy table", "Copy code"]) {
    const button = response.getByRole("button", { name: label, exact: true });
    await button.click();
    await expect(button).toHaveClass(/p-action-confirmed/);
    await expect(button).not.toHaveClass(/p-action-confirmed/);
  }
  const download = response.getByRole("button", {
    name: "Download Excel",
    exact: true,
  });
  await Promise.all([page.waitForEvent("download"), download.click()]);
  await expect(download).toHaveClass(/p-action-confirmed/);
  await expect(download).not.toHaveClass(/p-action-confirmed/);
  const active = page.locator(".p-chat-row.active button[aria-current=page]");
  await expect(active).toHaveCSS("background-color", "rgb(226, 232, 240)");
});

test("completed reasoning is consolidated above the answer with verified search sites", async ({
  page,
}) => {
  const directIcons: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (
      url.pathname === "/favicon.ico" &&
      !["127.0.0.1", "localhost"].includes(url.hostname)
    )
      directIcons.push(url.href);
  });
  await page.route("**/media/image?**", (route) =>
    route.fulfill({
      status: 200,
      contentType: "image/png",
      body: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1ioAAAAASUVORK5CYII=",
        "base64",
      ),
    }),
  );
  const chat = chatFixture("fixture");
  const message = {
    ...chat.data.messages.items[0]!,
    text: "A final public answer.",
    generationId: "run-fixture",
    activityKinds: ["plan", "web_research"],
    toolCheckpoints: [
      { offset: 0, operation: "web" },
      { offset: 10, operation: "web" },
    ],
  };
  const sites = [
    { origin: "https://example.com", result_count: 3 },
    { origin: "https://one.example.org", result_count: 2 },
    { origin: "https://two.example.org", result_count: 1 },
  ];
  const events = [
    {
      sequence: 1,
      kind: "activity",
      content: JSON.stringify({
        type: "web_research",
        id: "research-1",
        sites,
      }),
    },
    {
      sequence: 2,
      kind: "activity",
      content: JSON.stringify({
        type: "plan",
        operation: "upsert",
        agent_id: null,
        task: {
          id: "task-1",
          content: "Verify the public sources",
          active_form: "Verifying",
          status: "completed",
          parent_id: null,
        },
      }),
    },
  ];
  const record = {
    ...chat,
    data: { ...chat.data, messages: { items: [message], after: null } },
  };
  await mockDashboard(page, [], {
    activeChat: record,
    pengoloChat: record,
    pengoloEvents: {
      generationId: "run-fixture",
      status: "completed",
      events,
      message,
    },
    pengoloStream: {
      generationId: "run-fixture",
      status: "completed",
      events,
      message,
    },
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const article = page.getByRole("article", {
    name: "Pengolo response",
    exact: true,
  });
  const reasoning = article.getByText("Reasoning", { exact: true });
  await expect(reasoning).toHaveCount(1);
  expect((await reasoning.boundingBox())!.y).toBeLessThan(
    (await article.locator(".p-markdown").boundingBox())!.y,
  );
  await reasoning.click();
  await expect(
    article.getByText("Searched 3 websites", { exact: true }),
  ).toBeVisible();
  await expect(
    article.getByText("Verify the public sources", { exact: true }),
  ).toBeVisible();
  await expect(article.locator('img[src^="/media/image?url="]')).toHaveCount(3);
  expect(directIcons).toEqual([]);
  await expect(article).not.toContainText("↳");
  await expect(article.getByText("Thinking", { exact: true })).toHaveCount(0);
});

test("Pengolo keeps document redaction in the conversation tools", async ({
  page,
}) => {
  await mockDashboard(page);
  await page.goto("/pengolo");
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await expect(
    page.getByRole("menuitem", { name: "Redact a document", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("dialog", { name: "Redact a document", exact: true }),
  ).toHaveCount(0);
});

test("active chats have one uniform fill", async ({ page }) => {
  const record = chatFixture("fixture");
  await mockDashboard(page, [], {
    activeChat: record,
    pengoloChat: record,
    pengoloChats: { items: [record], after: null },
  });
  await page.goto("/pengolo/" + chatFixtureId);
  const row = page.locator(".p-chat-row.active");
  const selected = row.locator("button[aria-current=page]");
  await expect(row).toHaveCSS(
    "background-color",
    await selected.evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ),
  );
  await selected.hover();
  await expect(row).toHaveCSS(
    "background-color",
    await selected.evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ),
  );
});

test("prompt tools follow a single editable scroll surface", async ({
  page,
}) => {
  await mockDashboard(page);
  await page.goto("/pengolo");
  const prompt = page.getByRole("textbox", {
    name: "Message Pengolo",
    exact: true,
  });
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Web search", exact: true }).click();
  await prompt.fill("A prompt that wraps with its tool. ".repeat(200));
  const alignment = await prompt.evaluate((element) => {
    const prefix = element.previousElementSibling;
    const text = element.firstChild;
    if (!prefix || !text) throw new Error("Missing inline prompt contents");
    const range = document.createRange();
    range.setStart(text, 0);
    range.setEnd(text, 1);
    const glyph = range.getBoundingClientRect();
    const tool = prefix.getBoundingClientRect();
    return {
      difference: Math.abs(
        glyph.y + glyph.height / 2 - (tool.y + tool.height / 2),
      ),
      textX: glyph.x,
      toolX: tool.x,
    };
  });
  expect(alignment.difference).toBeLessThan(6);
  expect(alignment.textX).toBeGreaterThan(alignment.toolX);
  await prompt.press("ControlOrMeta+End");
  await expect
    .poll(() =>
      page
        .locator(".p-composer-entry")
        .evaluate((element) => element.scrollTop),
    )
    .toBeGreaterThan(0);
});

test("prompt pastes plain text, keeps newlines and copies without tool labels", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await mockDashboard(page);
  await page.goto("/pengolo");
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Web search", exact: true }).click();
  const prompt = page.getByRole("textbox", {
    name: "Message Pengolo",
    exact: true,
  });
  await prompt.fill("First line");
  await prompt.press("End");
  await prompt.press("Shift+Enter");
  await prompt.pressSequentially("Second line");
  await expect(prompt).toContainText("Second line");
  await prompt.press("ControlOrMeta+A");
  await prompt.press("ControlOrMeta+C");
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("First line\nSecond line");
  expect(copied).not.toContain("Web search");
  await prompt.evaluate((element) => {
    const clipboard = new DataTransfer();
    clipboard.setData("text/plain", "<b>literal</b>");
    clipboard.setData(
      "text/html",
      '<img src=x onerror="window.unexpectedPaste=true">',
    );
    element.dispatchEvent(
      new ClipboardEvent("paste", {
        clipboardData: clipboard,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
  await expect(prompt.locator("img,b")).toHaveCount(0);
});

test("prompt keeps a full-width focus target and a visible insertion line", async ({
  page,
}, testInfo) => {
  await mockDashboard(page);
  await page.goto("/pengolo");
  const prompt = page.getByRole("textbox", {
    name: "Message Pengolo",
    exact: true,
  });
  await page.getByRole("button", { name: "Add files and tools" }).click();
  await page.getByRole("menuitem", { name: "Web search", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Disable web search", exact: true }),
  ).toBeVisible();
  await page
    .locator(".p-composer-entry")
    .click({ position: { x: 350, y: 12 } });
  await expect(prompt).toBeFocused();
  await prompt.pressSequentially("bbb");
  await expect(prompt).toHaveText("bbb");
  await prompt.press("ArrowLeft");
  await prompt.pressSequentially("x");
  await expect(prompt).toHaveText("bbxb");
  await page.screenshot({
    path: testInfo.outputPath("composer-caret-short.png"),
    caret: "initial",
  });
  await prompt.fill("A longer prompt with public research. ".repeat(20));
  await prompt.press("ControlOrMeta+End");
  await page.screenshot({
    path: testInfo.outputPath("composer-caret-long.png"),
    caret: "initial",
  });
});

test("specialist report citations open an authorized panel on the right", async ({
  page,
}, info) => {
  const reportId = "a".repeat(64);
  const base = chatFixture();
  const record = {
    ...base,
    data: {
      ...base.data,
      messages: {
        ...base.data.messages,
        items: [
          {
            ...base.data.messages.items[0]!,
            generationId: "b".repeat(32),
            text: `Voir le [rapport sur les faits](#report-${reportId}).`,
          },
        ],
      },
    },
  };
  await mockDashboard(page, [], {
    activeChat: record,
    pengoloChat: record,
    pengoloChats: { items: [record], after: null },
    pengoloEvents: {
      generationId: "b".repeat(32),
      status: "completed",
      events: [],
      message: null,
      chatRevision: 1,
      chatUpdatedAt: 1,
      specialistReport: {
        id: reportId,
        agentId: "facts",
        content:
          "Rapport vérifiable.\n\n[Source citée](https://example.test/source)\n\n![Tracking](https://example.test/tracking.png)",
      },
    },
  });
  await page.goto("/pengolo/" + record.id);
  const citation = page.getByRole("link", { name: "rapport sur les faits" });
  await citation.click();
  const panel = page.getByRole("dialog", {
    name: "Specialist report",
    exact: true,
  });
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("Rapport vérifiable.");
  await expect(panel.locator(".p-markdown p").first()).toHaveCSS(
    "font-size",
    "14px",
  );
  const heading = panel.getByRole("heading", {
    name: "Specialist report",
    exact: true,
  });
  await expect
    .poll(() =>
      heading.evaluate((element) => {
        const box = element.getBoundingClientRect();
        return element.contains(
          document.elementFromPoint(
            box.x + box.width / 2,
            box.y + box.height / 2,
          ),
        );
      }),
    )
    .toBe(true);
  await expect(
    panel.locator("img,iframe,audio,video,object,embed"),
  ).toHaveCount(0);
  const box = await panel.boundingBox();
  expect(box!.x).toBeGreaterThan(600);
  await panel.screenshot({
    path: info.outputPath("specialist-report-panel.png"),
  });
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(citation).toBeFocused();
  await citation.click();
  await panel
    .getByRole("button", { name: "Close report", exact: true })
    .click();
  await expect(panel).toBeHidden();
  await citation.click();
  await page.locator(".p-header h1").click();
  await expect(panel).toBeHidden();
});

test("colored task cards keep model wording and animate their checked state", async ({
  page,
}, info) => {
  const base = chatFixture();
  const message = {
    ...base.data.messages.items[0]!,
    text: "| Point | Appréciation |\n| --- | --- |\n| Sources | ==À vérifier=={warning} |\n\n- [ ] Consulter les pièces\n- [x] Lire les références",
  };
  const record = {
    ...base,
    data: { ...base.data, messages: { items: [message], after: null } },
  };
  await mockDashboard(page, [], {
    activeChat: record,
    pengoloChat: record,
    pengoloChats: { items: [record], after: null },
    updatePengoloChat: {
      ...base,
      revision: 2,
      data: {
        ...base.data,
        messages: [{ ...message, taskChecks: [{ index: 0, checked: true }] }],
      },
    },
  });
  await page.goto("/pengolo/" + base.id);
  const article = page.getByRole("article", {
    name: "Pengolo response",
    exact: true,
  });
  await expect(article.locator("table")).toHaveCSS("font-size", "13px");
  await expect(article.locator("td").first()).toHaveCSS("padding-top", "8px");
  await expect(article.locator(".p-tone-warning")).toHaveText("À vérifier");
  await expect(article.locator(".p-tone-warning")).toHaveCSS(
    "color",
    "rgb(146, 64, 14)",
  );
  const checkbox = article.getByRole("checkbox", {
    name: "Consulter les pièces",
    exact: true,
  });
  await expect(checkbox).toHaveCSS("appearance", "none");
  await checkbox.check();
  await expect(checkbox).toBeChecked();
  const card = article.locator(".p-task-card").first();
  await expect(card).toHaveCSS("background-color", "rgb(255, 251, 235)");
  expect(
    await checkbox.evaluate(
      (element) => getComputedStyle(element, "::after").animationName,
    ),
  ).toBe("p-check");
  await article.screenshot({
    path: info.outputPath("colored-task-cards.png"),
    animations: "disabled",
  });
});

for (const entry of ["document", "password"] as const) {
  test(`MCP callback continues through document navigation after ${entry} sign-in`, async ({
    page,
  }) => {
    await mockDashboard(page);
    let continuations = 0;
    await page.route("**/auth/mcp/continue", (route) => {
      expect(route.request().isNavigationRequest()).toBe(true);
      continuations += 1;
      return route.fulfill({
        contentType: "text/html",
        body: "<!doctype html><title>MCP callback</title><h1>MCP client callback</h1>",
      });
    });
    if (entry === "document") {
      await page.goto("/login?mcp=1");
    } else {
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
            response.ok
          ) {
            if (options?.mode !== "same-origin")
              throw new Error("Credentials must stay same-origin");
            Object.defineProperty(response, "url", {
              value: `${location.origin}/login?mcp=1`,
            });
          }
          return response;
        };
      });
      await page.goto("/login");
      await page
        .getByRole("textbox", { name: "Work email", exact: true })
        .fill("ui@example.test");
      await page.getByRole("button", { name: "Continue", exact: true }).click();
      await page
        .locator('input[name="password"]')
        .fill("Fixture-only-password");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
    }
    await expect(
      page.getByRole("heading", { name: "MCP client callback" }),
    ).toBeVisible();
    expect(continuations).toBe(1);
  });
}
