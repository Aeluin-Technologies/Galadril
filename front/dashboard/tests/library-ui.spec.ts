import { test, expect } from "@playwright/test";

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
