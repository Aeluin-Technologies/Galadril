import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { SemanticDocument } from "../app/types/ontology";
import AxeBuilder from "@axe-core/playwright";

for (const decision of [true, false, "unavailable"] as const) {
  test(`email ontology navigation requires ontology creation permission: ${decision}`, async ({
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
    expect(checked).toBe(false);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollHeight <= innerHeight,
      ),
    ).toBe(true);
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollHeight <= innerHeight,
      ),
    ).toBe(true);
    const link = page.getByRole("link", {
      name: "Email ontology",
      exact: true,
    });
    await expect(link).toHaveCount(0);
  });
}

test("official ontology labels follow the locale while tenant names stay literal", async ({
  page,
}) => {
  await fixture(page);
  const shell = await page.request.get("/login");
  await page
    .context()
    .addCookies([
      { name: "locale", value: "fr", url: new URL(shell.url()).origin },
    ]);
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  await expect(
    page.getByRole("link", { name: "Organization people", exact: true }),
  ).toBeVisible({ timeout: 60000 });
  await page
    .getByRole("link", { name: "Courriel et documents", exact: true })
    .click();
  await page.getByRole("button", { name: "Personne", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Personne", exact: true }),
  ).toBeVisible();
});

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
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  await expect(
    page.getByRole("heading", { name: "All ontologies", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Organization people", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: /Email and documents/ }).click();
  await expect(
    page.getByRole("button", { name: "General overview", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Code", exact: true }).click();
  await page
    .getByRole("button", { name: "Plugin person", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Plugin person", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Plugin person", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Plugin person", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Email and documents", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Plugin person", exact: true })
    .click();
  await expect(
    page.getByRole("group", { name: "Contact connections", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Code", exact: true }).click();
  await expect(page.locator("[data-ontology-highlight]")).toContainText([
    "resource_id: mail.person",
  ]);
  await page
    .getByRole("navigation", { name: "Ontology location", exact: true })
    .getByRole("button", { name: "Email and documents", exact: true })
    .click();
  await page.getByText("Contact connections", { exact: true }).click();
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
  ).toHaveCount(0);
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

test("imported ontology layers expand and fold without editing the package", async ({
  page,
}, info) => {
  const state = await fixture(page, true, true);
  const shell = await page.request.get("/login");
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  await page
    .getByRole("link", { name: "Email and documents", exact: true })
    .click();
  const graph = page.getByRole("region", {
    name: "Related types",
    exact: true,
  });
  const layer = graph.getByRole("button", {
    name: "Shared foundations",
    exact: true,
  });
  await expect(layer).toHaveAttribute("aria-expanded", "false");
  await expect(graph.getByText("Plugin person", { exact: true })).toHaveCount(
    0,
  );
  await layer.click();
  await expect(layer).toHaveAttribute("aria-expanded", "true");
  await expect(graph.getByText("Plugin person", { exact: true })).toBeVisible();
  await layer.click();
  await expect(layer).toHaveAttribute("aria-expanded", "false");
  expect(state.writes).toBe(0);
  await graph.screenshot({
    path: info.outputPath("ontology-layers.png"),
    animations: "disabled",
  });
});

async function fixture(page: Page, canManage = true, layered = false) {
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
      {
        id: "utils.messages",
        ...(layered
          ? { package_id: "sample.core", package_name: "Shared foundations" }
          : {}),
        resources: [person, message, to, sender],
      },
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
  let preview: string | undefined;
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
    if (query.includes("ontologyWorkspace"))
      return route.fulfill({
        json: {
          data: {
            ontologyWorkspace: JSON.stringify({
              revision_id: v.id === savedId ? revision : "b".repeat(32),
              ontology:
                v.id === savedId && preview ? JSON.parse(preview) : tenant,
            }),
          },
        },
      });
    if (query.includes("pluginOntologyPreview")) {
      const mappings = JSON.parse(v.mappings);
      expect(mappings).toEqual({ "mail.message.to": "organization.person" });
      linked = true;
      preview = JSON.stringify({
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
      });
      return route.fulfill({
        json: { data: { pluginOntologyPreview: preview } },
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
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
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
    .getByRole("button", { name: "Add object type", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("Unfinished private type");
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "All ontologies", exact: true }),
  ).toHaveCount(0);
  await page.goBack();
  await expect(page).toHaveURL(/\/ontology$/);
  await page.goForward();
  await page
    .getByRole("button", { name: "Add object type", exact: true })
    .click();
  await expect(
    page.getByRole("textbox", { name: "Name", exact: true }),
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
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology/new?package=utils.mail");
  await expect(
    page.getByRole("button", { name: "General overview", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Plugin person", exact: true })
    .click();
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
  let saved: SemanticDocument | undefined;
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON();
    if (query.includes("ontologyWorkspace") && saved)
      return route.fulfill({
        json: {
          data: {
            ontologyWorkspace: { revision_id: "a".repeat(32), ontology: saved },
          },
        },
      });
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
    saved = definition;
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
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
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
  await expect(
    page.getByRole("textbox", { name: "Object type name", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Add object type", exact: true })
    .click();
  const wizard = page.getByRole("dialog", {
    name: "Create an object type",
    exact: true,
  });
  await expect(
    wizard.getByRole("button", { name: "Next", exact: true }),
  ).toBeDisabled();
  await wizard
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("Person");
  await wizard
    .getByRole("textbox", { name: "Definition", exact: true })
    .fill("A person involved in our work.");
  await wizard
    .getByRole("textbox", { name: "Identifier", exact: true })
    .fill("tenant.person");
  await wizard.getByRole("button", { name: "Next", exact: true }).click();
  await wizard
    .getByRole("textbox", { name: "Example", exact: true })
    .fill("Alice, project manager");
  await wizard.getByRole("button", { name: "Next", exact: true }).click();
  await wizard
    .getByRole("button", { name: "Create object type", exact: true })
    .click();
  await expect(wizard).not.toBeVisible();
  await page
    .getByRole("textbox", { name: "Property name", exact: true })
    .fill("Full name");
  await page.getByRole("button", { name: "Add property", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "Full name", exact: true }),
  ).toBeVisible();
  await page.getByRole("table").getByText("string", { exact: true }).hover();
  await expect(page.getByRole("tooltip")).toContainText(
    "Text, such as a name or an address.",
  );
  await page
    .getByRole("button", { name: "Add relationship", exact: true })
    .click();
  const relationship = page.getByRole("dialog", {
    name: "Relationship",
    exact: true,
  });
  await relationship
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("Knows");
  await relationship
    .getByRole("combobox", { name: "Target type", exact: true })
    .click();
  await page.getByRole("option", { name: "Person", exact: true }).click();
  await relationship
    .getByRole("button", { name: "Save relationship", exact: true })
    .click();
  await expect(relationship).not.toBeVisible();
  await page.getByText("Knows", { exact: true }).click();
  await expect(relationship).toBeVisible();
  await relationship
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("Colleague");
  await relationship
    .getByRole("button", { name: "Save relationship", exact: true })
    .click();
  await expect(page.getByText("Colleague", { exact: true })).toBeVisible();
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
  ).toHaveCount(0);
  await expect(
    page.getByRole("textbox", { name: "Property name", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Code", exact: true }).click();
  await expect(
    page.getByRole("textbox", { name: "Ontology YAML", exact: true }),
  ).toHaveCount(0);
  const editGraph = page.getByRole("button", {
    name: "Edit graph",
    exact: true,
  });
  await editGraph.click();
  await expect(editGraph).toHaveAttribute("aria-pressed", "true");
  const code = page.getByRole("textbox", {
    name: "Ontology YAML",
    exact: true,
  });
  const savedCode = await code.inputValue();
  await code.fill("resources: [");
  await page
    .getByRole("button", { name: "Apply YAML to draft", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  expect(writes).toBe(1);
  await code.fill(savedCode.replace("Full name", "Legal name"));
  await page
    .getByRole("button", { name: "Apply YAML to draft", exact: true })
    .click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByRole("button", { name: "Graph", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "Legal name", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Review changes", exact: true }),
  ).toBeEnabled();
  expect(writes).toBe(1);
});

test("authorized ontologies are searchable and open independently of plugin or pipeline access", async ({
  page,
}) => {
  await fixture(page, false);
  let revoked = false;
  await page.route("**/graphql", (route) => {
    const { query } = route.request().postDataJSON();
    if (query.includes("globalSearchPage"))
      return route.fulfill({
        json: {
          data: {
            globalSearchPage: { items: [], navigation: [], after: null },
          },
        },
      });
    if (
      query.includes("pluginOntologies") ||
      query.includes("pipelineDefinitions")
    )
      return route.fulfill({
        json: { errors: [{ extensions: { code: "FORBIDDEN" } }] },
      });
    if (revoked && query.includes("{ontologies"))
      return route.fulfill({ json: { data: { ontologies: [] } } });
    return route.fallback();
  });
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology");
  await page
    .getByRole("button", { name: "Quick search or actions...", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Search Galadril",
    exact: true,
  });
  const search = dialog.getByRole("textbox", {
    name: "Search Galadril",
    exact: true,
  });
  await search.fill("ontology");
  await expect(dialog.getByText("Ontologies", { exact: true })).toBeVisible();
  await search.fill("people");
  await dialog.getByText("Organization people", { exact: true }).click();
  await expect(page).toHaveURL(/\/ontology\/edit\/people$/);
  await page.getByRole("button", { name: "Person", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Person", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Edit ontology", exact: true }),
  ).toBeDisabled();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page
    .getByRole("button", { name: "Quick search or actions...", exact: true })
    .click();
  await search.fill("people");
  await expect(
    dialog.getByText("Organization people", { exact: true }),
  ).toBeVisible();
  revoked = true;
  await page.evaluate(() =>
    window.dispatchEvent(new Event("pengolo-access-invalidated")),
  );
  await expect(search).toHaveValue("");
  await expect(
    dialog.getByText("Organization people", { exact: true }),
  ).toHaveCount(0);
  await search.fill("people");
  await expect(
    dialog.getByText("Organization people", { exact: true }),
  ).toHaveCount(0);
});

test("graph connectors open a relationship editor without persisting a draft", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.route("**/graphql", (route) => {
    const { query } = route.request().postDataJSON();
    if (!query.includes("ontologyWorkspace")) return route.fallback();
    return route.fulfill({
      json: {
        data: {
          ontologyWorkspace: JSON.stringify({
            revision_id: "b".repeat(32),
            ontology: {
              version: "1",
              resources: [
                {
                  resource_id: "organization.person",
                  kind: "object_type",
                  display_name: "Person",
                  references: [],
                  attributes: {},
                },
                {
                  resource_id: "organization.document",
                  kind: "object_type",
                  display_name: "Document",
                  references: [],
                  attributes: {},
                },
              ],
            },
          }),
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
  const shell = await page.request.get("/login");
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology/edit/people");
  await expect(
    page.getByRole("region", {
      name: "Pipeline ontology binding",
      exact: true,
    }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Edit graph", exact: true }).click();
  const source = page.locator(
    '.vue-flow__node[data-id="organization.person"] .vue-flow__handle.source',
  );
  const target = page.locator(
    '.vue-flow__node[data-id="organization.document"] .vue-flow__handle.target',
  );
  await expect(source).toBeVisible();
  const from = await source.boundingBox(),
    to = await target.boundingBox();
  if (!from || !to) throw new Error("Missing graph connectors");
  await page.mouse.move(from.x + from.width / 2, from.y + from.height / 2);
  await page.mouse.down();
  await page.mouse.move(to.x + to.width / 2, to.y + to.height / 2, {
    steps: 12,
  });
  await page.mouse.up();
  const editor = page.getByRole("dialog", {
    name: "Relationship",
    exact: true,
  });
  await expect(editor).toBeVisible();
  await expect(
    editor.getByRole("combobox", { name: "Source type", exact: true }),
  ).toContainText("Person");
  await expect(
    editor.getByRole("combobox", { name: "Target type", exact: true }),
  ).toContainText("Document");
  expect(state.writes).toBe(0);
});

test("ontology branches and history are separate from publication and trash respects plugin provenance", async ({
  page,
}) => {
  await fixture(page);
  const model: SemanticDocument = { version: "1", resources: [] };
  const workspace = {
    revision_id: "b".repeat(32),
    ontology: model,
    display_name: "Organization people",
    authoring: { branches: {} as Record<string, unknown> },
  };
  const commands: string[] = [];
  await page.route("**/graphql", async (route) => {
    const { query, variables: v } = route.request().postDataJSON();
    if (query.includes("ontologyWorkspace"))
      return route.fulfill({
        json: { data: { ontologyWorkspace: workspace } },
      });
    if (query.includes("authorOntology")) {
      const command = JSON.parse(v.command);
      commands.push(command.operation);
      expect(v.expected).toBe(workspace.revision_id);
      if (command.operation === "create_branch")
        workspace.authoring.branches[command.branch] = {
          parent_branch: "main",
          definition: model,
          display_name: "Experiment",
          base_revision_id: workspace.revision_id,
          author: "author",
          updated_at_ms: 12,
        };
      if (command.operation === "delete_branch")
        workspace.authoring.branches = {};
      return route.fulfill({ json: { data: { authorOntology: workspace } } });
    }
    if (query.includes("ontologyHistory"))
      return route.fulfill({
        json: {
          data: {
            ontologyHistory: {
              items: [
                {
                  revision_id: "a".repeat(32),
                  author: "author",
                  message: "Initial model",
                  branch: "main",
                  operation: "save_draft",
                  created_at_ms: 12,
                },
              ],
              next_revision_id: null,
            },
          },
        },
      });
    return route.fallback();
  });
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route(/\/ontology(?:\/[^?]*)?(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/ontology/edit/people");
  await expect(
    page.getByRole("button", { name: "Delete ontology", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Branches", exact: true }).click();
  await page.getByRole("button", { name: "New branch", exact: true }).click();
  const dialog = page.getByRole("dialog", {
    name: "Create branch",
    exact: true,
  });
  await dialog
    .getByRole("textbox", { name: "Branch name", exact: true })
    .fill("experiment");
  await dialog
    .getByRole("button", { name: "Create branch", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "experiment", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await page
    .getByRole("button", { name: "Delete branch", exact: true })
    .click();
  const confirmation = page.getByRole("dialog", {
    name: "Delete branch",
    exact: true,
  });
  await confirmation
    .getByRole("button", { name: "Cancel", exact: true })
    .click();
  expect(commands).toEqual(["create_branch"]);
  await page.getByRole("button", { name: "History", exact: true }).click();
  await page
    .getByRole("button", { name: "Initial model", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "Ontology revision", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Close dialog", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Branches", exact: true }),
  ).toBeVisible();
  workspace.ontology = {
    ...model,
    plugin_origin: {
      package_id: "utils.mail",
      package_version: "1.0.0",
      plugin_version: "1.0.0",
    },
  };
  await page.reload();
  await expect(
    page.getByRole("button", { name: "Delete ontology", exact: true }),
  ).toHaveCount(0);
});
