import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

for (const decision of [true, false, "unavailable"] as const) {
  test(`email data model navigation requires ontology creation permission: ${decision}`, async ({
    page,
  }) => {
    await fixture(page);
    let checked = false;
    await page.route("**/graphql", (route) => {
      const { query } = route.request().postDataJSON();
      if (query.includes("ontologyCanManage")) {
        checked = true;
        return route.fulfill({
          json:
            decision === "unavailable"
              ? { errors: [{ message: "unavailable" }] }
              : { data: { ontologyCanManage: decision } },
        });
      }
      if (query.includes("emailOperation"))
        return route.fulfill({ json: { data: { emailOperation: "[]" } } });
      return route.fallback();
    });
    await page.addInitScript(() =>
      localStorage.setItem(
        "galadril_onboarding_state",
        JSON.stringify({ isTutorialCompleted: true }),
      ),
    );
    const shell = await page.request.get("/login");
    await page.route("**/settings/user", (route) =>
      route.request().isNavigationRequest()
        ? route.fulfill({ response: shell })
        : route.continue(),
    );
    await page.goto("/settings/user#email");
    await expect(
      page.getByRole("button", {
        name: "Connect an email account",
        exact: true,
      }),
    ).toBeVisible();
    await expect.poll(() => checked).toBe(true);
    const link = page.getByRole("link", {
      name: "Email data model",
      exact: true,
    });
    if (decision === true) await expect(link).toBeVisible();
    else await expect(link).toHaveCount(0);
  });
}

test("plugin ontologies stay isolated until an explicit tenant participant mapping", async ({
  page,
}, testInfo) => {
  const state = await fixture(page);
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route("**/ontology*", (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  await page.getByRole("button", { name: /Email and documents/ }).click();
  await expect(
    page.getByRole("heading", { name: "Plugin person", exact: true }),
  ).toBeVisible();
  expect(state.writes).toBe(0);
  expect(state.linked).toBe(false);
  await page
    .getByRole("combobox", { name: "Target ontology", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Organization people", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Recipient type", exact: true })
    .click();
  await page.getByRole("option", { name: "Person", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "Sender type", exact: true }),
  ).toContainText("Plugin Person (isolated)");
  await page
    .getByRole("button", { name: "Review changes", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Review ontology changes" }),
  ).toBeVisible();
  expect(state.writes).toBe(0);
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect.poll(() => state.writes).toBe(1);
  expect(state.savedId).toMatch(/^utils-mail-/);
  await expect(
    page.getByRole("status").filter({ hasText: "Draft saved" }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("ontology-desktop.png"),
    fullPage: true,
  });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("ontology-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

async function fixture(page: Page, canManage = true) {
  const person = {
    resource_id: "mail.person",
    kind: "object_type",
    display_name: "Plugin person",
    references: [],
    attributes: {},
  };
  const message = {
    ...person,
    resource_id: "mail.message",
    display_name: "Message",
  };
  const to = {
    resource_id: "mail.message.to",
    kind: "link_type",
    display_name: "Recipient",
    references: ["mail.message", "mail.person"],
    attributes: {
      source_type: "mail.message",
      target_type: "mail.person",
      mapping_slot: true,
    },
  };
  const sender = {
    ...to,
    resource_id: "mail.message.sender",
    display_name: "Sender",
  };
  const pkg = {
    id: "utils.mail",
    plugin_id: "utils",
    plugin_version: "1.0.0",
    version: "1.0.0",
    name: "Email and documents",
    ontologies: [
      { id: "utils.messages", resources: [person, message, to, sender] },
    ],
  };
  const tenant = {
    version: "1",
    resources: [
      { ...person, resource_id: "organization.person", display_name: "Person" },
    ],
  };
  let writes = 0;
  let linked = false;
  let savedId = "";
  const revision = "a".repeat(32);
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        subject: "author",
        tenant: "tenant",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        requiresPasskey: false,
      },
    }),
  );
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage(() => {}),
  );
  await page.route("**/graphql", (route) => {
    const { query, variables: v } = route.request().postDataJSON();
    if (query.includes("ontologyCanManage"))
      return route.fulfill({
        json: { data: { ontologyCanManage: canManage } },
      });
    if (query.includes("pluginOntologies"))
      return route.fulfill({
        json: {
          data: { pluginOntologies: JSON.stringify({ packages: [pkg] }) },
        },
      });
    if (query.includes("pipelineDefinitions"))
      return route.fulfill({ json: { data: { pipelineDefinitions: [] } } });
    if (query.includes("{ontologies{"))
      return route.fulfill({
        json: {
          data: {
            ontologies: [
              {
                ontologyId: "people",
                displayName: "Organization people",
                productionPublication: {
                  revisionId: revision,
                  publicationId: "p",
                },
              },
            ],
          },
        },
      });
    if (query.includes("ontologyDocument"))
      return route.fulfill({
        json: { data: { ontologyDocument: JSON.stringify(tenant) } },
      });
    if (query.includes("pluginOntologyPreview")) {
      const mappings = JSON.parse(v.mappings);
      expect(mappings).toEqual({ "mail.message.to": "organization.person" });
      linked = true;
      return route.fulfill({
        json: {
          data: {
            pluginOntologyPreview: JSON.stringify({
              version: "1.0.0",
              resources: [
                person,
                message,
                {
                  ...to,
                  attributes: {
                    ...to.attributes,
                    target_type: "organization.person",
                  },
                },
                sender,
                ...tenant.resources,
              ],
              participant_mappings: mappings,
              plugin_origin: {
                package_id: pkg.id,
                package_version: pkg.version,
              },
            }),
          },
        },
      });
    }
    if (query.includes("savePluginOntology")) {
      writes++;
      expect(v.packageVersion).toBe("1.0.0");
      expect(v.targetId).toBe("people");
      expect(v.targetRevision).toBe(revision);
      expect(JSON.parse(v.mappings)).toEqual({
        "mail.message.to": "organization.person",
      });
      savedId = v.id;
      return route.fulfill({
        json: { data: { savePluginOntology: revision } },
      });
    }
    return route.fulfill({ json: { data: {} } });
  });
  return {
    get writes() {
      return writes;
    },
    get linked() {
      return linked;
    },
    get savedId() {
      return savedId;
    },
  };
}

test("leaving the ontology workspace clears unfinished type fields", async ({
  page,
}) => {
  await fixture(page);
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route("**/ontology*", (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  const create = page.getByRole("button", {
    name: "New ontology",
    exact: true,
  });
  await expect(create).toBeEnabled();
  await create.click();
  await page
    .getByRole("textbox", { name: "Object type name", exact: true })
    .fill("Unfinished private type");
  await page.getByRole("link", { name: "All pipelines", exact: true }).click();
  await expect(page).toHaveURL(/\/pipeline$/);
  await page.goBack();
  await expect(create).toBeEnabled();
  await create.click();
  await expect(
    page.getByRole("textbox", { name: "Object type name", exact: true }),
  ).toHaveValue("");
});

test("ontology viewers can inspect plugin types without editing the tenant copy", async ({
  page,
}) => {
  await fixture(page, false);
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route("**/ontology*", (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology?package=utils.mail");
  await expect(
    page.getByRole("heading", { name: "Plugin person", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Review changes", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "New ontology", exact: true }),
  ).toBeDisabled();
});

test("an administrator defines tenant types and reviews them before saving", async ({
  page,
}) => {
  await fixture(page);
  let writes = 0;
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON();
    if (!query.includes("saveOntologyDraft")) return route.fallback();
    const definition = JSON.parse(variables.definition);
    expect(definition.plugin_origin).toBeUndefined();
    expect(definition.resources).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          resource_id: "tenant.person",
          kind: "object_type",
          display_name: "Person",
        }),
        expect.objectContaining({
          owner_id: "tenant.person",
          kind: "property",
          display_name: "Full name",
          value_type: "string",
        }),
      ]),
    );
    writes++;
    return route.fulfill({
      json: { data: { saveOntologyDraft: "a".repeat(32) } },
    });
  });
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route("**/ontology*", (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  const create = page.getByRole("button", {
    name: "New ontology",
    exact: true,
  });
  await expect(create).toBeEnabled();
  await create.click();
  await page
    .getByRole("textbox", { name: "Ontology name", exact: true })
    .fill("People");
  await page
    .getByRole("textbox", { name: "Object type name", exact: true })
    .fill("Person");
  await page
    .getByRole("textbox", { name: "Object type identifier", exact: true })
    .fill("tenant.person");
  await page
    .getByRole("button", { name: "Add object type", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Property name", exact: true })
    .fill("Full name");
  await page.getByRole("button", { name: "Add property", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "Full name", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Review changes", exact: true })
    .click();
  expect(writes).toBe(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(writes).toBe(0);
  await page
    .getByRole("button", { name: "Review changes", exact: true })
    .click();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect.poll(() => writes).toBe(1);
  await expect(
    page.getByRole("status").filter({ hasText: "Draft saved" }),
  ).toBeVisible();
});
