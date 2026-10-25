import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("library selection groups actions, hides completed progress and starts an attachment-only draft", async ({
  page,
}, testInfo) => {
  let files = ["Evidence one", "Evidence two"].map((body, index) => ({
    id: String(index + 1).repeat(32),
    ownerId: "owner",
    projectId: null,
    revision: 1,
    updatedAt: 1,
    expiresAt: Date.now() + 86400000,
    data: {
      name: `Evidence ${index + 1}.txt`,
      mime: "text/plain",
      size: body.length,
      status: "ready",
      temporarySession: null,
      indexing: null,
      retrieval: { status: "ready", profileId: null, warnings: [] },
    },
  }));
  let deleted = 0;
  const project = {
    id: "c".repeat(32),
    ownerId: "owner",
    projectId: null,
    revision: 1,
    updatedAt: 1,
    expiresAt: null,
    data: {
      name: "Research",
      icon: "🧭",
      color: "#2563eb",
      shared: false,
      roles: [],
      contextMode: "project",
    },
  };
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        subject: "owner",
        tenant: "tenant",
        displayName: "Owner",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        requiresPasskey: false,
      },
    }),
  );
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage(() => {}),
  );
  await page.route("**/fixture-file/*", (route) =>
    route.fulfill({
      body: route.request().url().endsWith("1")
        ? "Evidence one"
        : "Evidence two",
      contentType: "text/plain",
    }),
  );
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON();
    let data: Record<string, unknown> = {};
    if (query.includes("query Workspace"))
      data = {
        pengoloIdentity: {
          version: "1",
          administrator: false,
          webSearchAvailable: false,
        },
        pengoloModels: {
          defaultModel: "local",
          models: [
            {
              alias: "local",
              model: "local",
              provider: "configured",
              reasoningEfforts: ["medium"],
            },
          ],
        },
        pengoloProjects: [project],
        pengoloChats: { items: [], after: null },
      };
    else if (query.includes("pengoloLibrary"))
      data = { pengoloLibrary: { items: files, after: null } };
    else if (query.includes("pengoloDownload"))
      data = {
        pengoloDownload: {
          url: `/fixture-file/${variables.input.id.startsWith("1") ? "1" : "2"}`,
        },
      };
    else if (query.includes("deletePengoloFile")) {
      deleted++;
      files = files.filter((file) => file.id !== variables.input.id);
      data = { deletePengoloFile: true };
    } else if (query.includes("pengoloChats"))
      data = { pengoloChats: { items: [], after: null } };
    else if (query.includes("pengoloOpenai"))
      data = {
        pengoloOpenai: {
          enabled: false,
          connected: false,
          quotaExhausted: false,
          selected: null,
          usageUrl: null,
          authorizationUrl: null,
          revocationConfirmed: null,
          accounts: [],
        },
      };
    return route.fulfill({ json: { data } });
  });
  const shell = await page.request.get("/login");
  await page.route(/\/pengolo(?:\/library)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  await page.goto("/pengolo/library");
  const library = page.getByRole("region", {
    name: "File library",
    exact: true,
  });
  await expect(
    library.getByText("Document indexed", { exact: true }),
  ).toHaveCount(2);
  await expect(library.getByRole("progressbar")).toHaveCount(0);
  await expect(
    library.getByText("Refresh search index", { exact: true }),
  ).toHaveCount(0);
  await library
    .getByRole("button", { name: "Actions for Evidence 1.txt", exact: true })
    .click();
  await expect(
    page.getByRole("menuitem", { name: "Reindex", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await library
    .getByRole("checkbox", { name: "Select Evidence 1.txt", exact: true })
    .check();
  await library
    .getByRole("checkbox", { name: "Select Evidence 2.txt", exact: true })
    .check();
  await expect(
    library.getByRole("toolbar", { name: "Selected documents", exact: true }),
  ).toContainText("2 selected");
  const download = page.waitForEvent("download");
  await library
    .getByRole("button", { name: "Download selected", exact: true })
    .click();
  expect((await download).suggestedFilename()).toBe("Galadril-documents.zip");
  await library
    .getByRole("button", { name: "Start chat", exact: true })
    .click();
  await expect(page).toHaveURL(/\/pengolo$/);
  await expect(
    page.getByRole("button", { name: "Remove Evidence 1.txt", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Remove Evidence 2.txt", exact: true }),
  ).toBeVisible();
  await page.goto("/pengolo/library");
  await library
    .getByRole("checkbox", { name: "Select all documents", exact: true })
    .check();
  await page.screenshot({
    path: testInfo.outputPath("library-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("library-narrow.png"),
    fullPage: true,
  });
  await library
    .getByRole("button", { name: "Delete selected", exact: true })
    .click();
  expect(deleted).toBe(0);
  const confirm = page.getByRole("dialog", {
    name: "Delete selected documents?",
    exact: true,
  });
  await confirm
    .getByRole("button", { name: "Delete documents", exact: true })
    .click();
  await expect(
    library.getByText("No files yet", { exact: true }),
  ).toBeVisible();
  expect(deleted).toBe(2);
});

test("projects use colored Heroicons and confirm owner deletion", async ({
  page,
}) => {
  let removed = false;
  const project = {
    id: "c".repeat(32),
    ownerId: "owner",
    projectId: null,
    revision: 1,
    updatedAt: 1,
    expiresAt: null,
    data: {
      name: "Research",
      icon: "🧭",
      color: "#2563eb",
      shared: false,
      roles: [],
      contextMode: "project",
    },
  };
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        subject: "owner",
        tenant: "tenant",
        displayName: "Owner",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        requiresPasskey: false,
      },
    }),
  );
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage(() => {}),
  );
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON();
    let data: Record<string, unknown> = {};
    if (query.includes("query Workspace"))
      data = {
        pengoloIdentity: {
          version: "1",
          administrator: false,
          webSearchAvailable: false,
        },
        pengoloModels: {
          defaultModel: "local",
          models: [
            {
              alias: "local",
              model: "local",
              provider: "configured",
              reasoningEfforts: ["medium"],
            },
          ],
        },
        pengoloProjects: [project],
        pengoloChats: { items: [], after: null },
      };
    else if (query.includes("deletePengoloProject")) {
      expect(variables.input).toEqual({ id: project.id, revision: 1 });
      removed = true;
      data = { deletePengoloProject: true };
    } else if (query.includes("pengoloOpenai"))
      data = {
        pengoloOpenai: {
          enabled: false,
          connected: false,
          quotaExhausted: false,
          accounts: [],
        },
      };
    else if (query.includes("pengoloChats"))
      data = { pengoloChats: { items: [], after: null } };
    return route.fulfill({ json: { data } });
  });
  const shell = await page.request.get("/login");
  await page.route("**/pengolo", (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  await page.goto("/pengolo");
  const row = page.locator(".p-project").filter({ hasText: "Research" });
  await expect(row.locator('[data-project-icon="compass"] svg')).toBeVisible();
  expect(
    await row
      .locator('[data-project-icon="compass"] svg')
      .evaluate((element) => getComputedStyle(element).color),
  ).toBe("rgb(37, 99, 235)");
  await row
    .getByRole("button", { name: "Options for project Research", exact: true })
    .click();
  await page
    .getByRole("menuitem", { name: "Rename and permissions", exact: true })
    .click();
  const editor = page.getByRole("dialog", {
    name: "Edit project",
    exact: true,
  });
  await editor
    .getByRole("button", { name: "Customize project appearance", exact: true })
    .click();
  await expect(
    editor.getByRole("textbox", { name: "Custom project emoji", exact: true }),
  ).toHaveCount(0);
  await expect(
    editor.getByRole("button", { name: "Use Scale icon", exact: true }),
  ).toBeVisible();
  await editor.getByRole("button", { name: "Cancel", exact: true }).click();
  await row
    .getByRole("button", { name: "Options for project Research", exact: true })
    .click();
  const action = page.getByRole("menuitem", {
    name: "Delete project",
    exact: true,
  });
  await expect(action.locator("svg")).toHaveCount(1);
  await action.click();
  expect(removed).toBe(false);
  const confirmation = page.getByRole("dialog", {
    name: "Delete project?",
    exact: true,
  });
  await confirmation
    .getByRole("button", { name: "Delete project", exact: true })
    .click();
  await expect(row).toHaveCount(0);
  expect(removed).toBe(true);
});
