import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("pipeline drafts are listed, saved with revisions, published and retired through Gateway", async ({
  page,
}) => {
  let name = "Correspondence";
  let revision = "a".repeat(32);
  let published: string | null = null;
  let deleted = false;
  let conflict = false;
  const definition = () => ({
    name,
    sources: [
      {
        id: "mail",
        topic: "raw",
        match_pattern: ".*",
        schema_path: "schemas/avro/email.avsc",
        source_kind: "email",
      },
    ],
    pipeline: [
      { step: "graph", type: "sink", input_from: ["mail"], params: {} },
    ],
  });
  const artifact = () => ({
    pipeline_id: "mail",
    name,
    owner_id: "author",
    head_revision_id: revision,
    published_revision_id: published,
    definition: JSON.stringify(definition()),
    message: "Initial version",
    author_id: "author",
    updated_at_ms: 1,
  });
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
    const body = route.request().postDataJSON();
    if (body.query.includes("updatePipeline")) {
      expect(body.variables.expected).toBe(revision);
      if (conflict)
        return route.fulfill({
          json: {
            data: null,
            errors: [{ extensions: { code: "PIPELINE_CONFLICT" } }],
          },
        });
      name = body.variables.name;
      expect(JSON.parse(body.variables.definition).name).toBe(name);
      revision = "b".repeat(32);
      return route.fulfill({ json: { data: { updatePipeline: artifact() } } });
    }
    if (body.query.includes("publishPipeline")) {
      expect(body.variables.revision).toBe(revision);
      published = revision;
      return route.fulfill({ json: { data: { publishPipeline: artifact() } } });
    }
    if (body.query.includes("deletePipeline")) {
      expect(body.variables.expected).toBe(revision);
      deleted = true;
      return route.fulfill({ json: { data: { deletePipeline: true } } });
    }
    if (body.query.includes("pipelineDefinitions"))
      return route.fulfill({
        json: { data: { pipelineDefinitions: deleted ? [] : [artifact()] } },
      });
    if (body.query.includes("history(after:"))
      return route.fulfill({
        json: {
          data: {
            pipelineDefinition: {
              history: {
                items: [
                  {
                    revision_id: revision,
                    name,
                    message: "Saved revision",
                    author_id: "author",
                    updated_at_ms: 1,
                  },
                ],
                after: null,
              },
            },
          },
        },
      });
    if (body.query.includes("pipelineDefinition("))
      return route.fulfill({
        json: { data: { pipelineDefinition: artifact() } },
      });
    if (body.query.includes("pipelineComposition"))
      return route.fulfill({
        json: {
          data: {
            pipelineComposition: {
              definition: body.variables.definition,
              blocks: 2,
              transfers: 0,
            },
          },
        },
      });
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route(/\/pipeline(?:\/[^/?]+)?(?:\?.*)?$/, (route) =>
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
  await page.goto("/pipeline");
  await page.getByRole("link", { name: "Correspondence", exact: true }).click();
  const field = page.getByRole("textbox", {
    name: "Pipeline Name",
    exact: true,
  });
  await expect(field).toHaveValue("Correspondence");
  await field.fill("Mail analysis");
  await page.getByRole("button", { name: "Save version", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "Version saved" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Publish pipeline", exact: true })
    .click();
  await expect(page.getByText("Published", { exact: true })).toBeVisible();
  await page.getByText("Version details", { exact: true }).click();
  await expect(page.getByText("Saved revision", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Load this version", exact: true })
    .click();
  await expect(field).toHaveValue("Mail analysis");
  conflict = true;
  await field.fill("Keep this draft");
  await page.getByRole("button", { name: "Save version", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("could not be saved");
  await expect(field).toHaveValue("Keep this draft");
  await page
    .getByRole("button", { name: "Delete pipeline", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Delete pipeline", exact: true })
    .click();
  await expect(page).toHaveURL(/\/pipeline$/);
  await expect(
    page.getByText("No pipelines yet", { exact: true }),
  ).toBeVisible();
});

test("authors can link, connect, inspect, and validate a pinned pipeline", async ({
  page,
}, testInfo) => {
  page.on("pageerror", (error) => console.error("Browser error:", error.stack));
  let denied = false;
  const revision = "a".repeat(32);
  const definition = {
    name: "Legal",
    sources: [
      {
        id: "mail",
        topic: "raw",
        match_pattern: ".*",
        schema_path: "text.avsc",
      },
    ],
    pipeline: [
      { step: "legal_report", type: "sink", input_from: ["mail"], params: {} },
    ],
  };
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
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage(() => {}),
  );
  await page.route("**/graphql", (route) => {
    const body = route.request().postDataJSON();
    if (body.query.includes("scribeModels"))
      return route.fulfill({
        json: {
          data: {
            scribeModels: {
              models: [
                { alias: "local-legal", model: "local/legal" },
                { alias: "api-legal", model: "api/legal" },
              ],
            },
          },
        },
      });
    if (body.query.includes("pipelineLinkTargets"))
      return route.fulfill({
        json: {
          data: {
            pipelineLinkTargets: [
              {
                pipeline_id: "law",
                name: "Legal",
                head_revision_id: revision,
                published_revision_id: revision,
              },
            ],
          },
        },
      });
    if (body.query.includes("pipelineDefinition"))
      return route.fulfill({
        json: denied
          ? {
              data: { pipelineDefinition: null },
              errors: [
                {
                  extensions: { code: "FORBIDDEN" },
                  path: ["pipelineDefinition"],
                },
              ],
            }
          : {
              data: {
                pipelineDefinition: {
                  pipeline_id: "law",
                  name: "Legal",
                  head_revision_id: revision,
                  published_revision_id: revision,
                  definition: JSON.stringify(definition),
                },
              },
            },
      });
    if (body.query.includes("pipelineComposition")) {
      const authored = JSON.parse(body.variables.definition);
      expect(
        authored.pipeline.find(
          (step: { type: string }) => step.type === "pipeline",
        ).pipeline_ref.revision_id,
      ).toBe(revision);
      return route.fulfill({
        json: {
          data: {
            pipelineComposition: {
              definition: JSON.stringify(definition),
              blocks: 4,
              transfers: 1,
            },
          },
        },
      });
    }
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route("**/pipeline/new", (route) =>
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
  await page.goto("/pipeline/new");
  const initialInput = page.locator('[data-step-kind="source"]');
  await expect(initialInput).toBeVisible({ timeout: 60000 });
  await expect
    .poll(async () => (await initialInput.boundingBox())?.width ?? 0)
    .toBeLessThanOrEqual(270);
  await page.getByRole("button", { name: "Add a block", exact: true }).click();
  await page.getByRole("menuitem", { name: /Use another pipeline/ }).click();
  const picker = page.getByRole("dialog", { name: "Choose a pipeline" });
  await picker.getByRole("combobox", { name: "Pipeline", exact: true }).click();
  await page.getByRole("option", { name: "Legal", exact: true }).click();
  await picker
    .getByRole("button", { name: "Add pipeline", exact: true })
    .click();
  await expect(picker).not.toBeVisible();
  await expect(page.locator('[data-step-kind="pipeline"]')).toBeVisible();
  await page.getByRole("button", { name: "Add a block", exact: true }).focus();
  await page.keyboard.press("ControlOrMeta+z");
  await expect(page.locator('[data-step-kind="pipeline"]')).toHaveCount(0);
  await page.keyboard.press("ControlOrMeta+Shift+z");
  await expect(page.locator('[data-step-kind="pipeline"]')).toHaveCount(1);
  const node = page.locator('[data-step-kind="pipeline"]');
  await node.getByRole("button", { name: "Configure block" }).click();
  const settings = page.getByRole("dialog", { name: "Configure block" });
  await settings.getByRole("combobox", { name: "Incoming block" }).click();
  await page.getByRole("option", { name: "input", exact: true }).click();
  await settings.getByRole("button", { name: "Apply changes" }).click();
  await node.getByRole("button", { name: "Show linked pipeline" }).click();
  await expect(node.getByText("legal_report", { exact: true })).toBeVisible();
  await node.getByRole("button", { name: "Hide linked pipeline" }).click();
  denied = true;
  await node.getByRole("button", { name: "Show linked pipeline" }).click();
  await expect(node.getByRole("alert")).toBeVisible();
  await expect(node.getByText("legal_report", { exact: true })).toHaveCount(0);
  denied = false;
  await page
    .getByRole("button", { name: "Check pipeline", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Composition validated" }),
  ).toBeVisible();
  const violations = (
    await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze()
  ).violations;
  expect(violations).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("pipeline-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: "Add a block", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("region", { name: "Pipeline canvas", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(node).toBeInViewport({ ratio: 0.5 });
  await page.screenshot({
    path: testInfo.outputPath("pipeline-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "Add a block", exact: true }).click();
  await page.getByRole("menuitem", { name: /Route content/ }).click();
  const router = page.locator('[data-step-kind="route"]');
  await router.getByRole("button", { name: "Configure block" }).click();
  await settings.getByRole("combobox", { name: "Routing method" }).click();
  await page.getByRole("option", { name: "Expected metadata values" }).click();
  await settings
    .getByRole("textbox", { name: "Metadata key", exact: true })
    .fill("record.metadata.date");
  await settings
    .getByRole("textbox", { name: "Expected value", exact: true })
    .fill("2026-10-23");
  await settings.getByRole("button", { name: "Apply changes" }).click();
  await router.getByRole("button", { name: "Configure block" }).click();
  await expect(
    settings.getByRole("textbox", { name: "Expected value", exact: true }),
  ).toHaveValue("2026-10-23");
  await settings.getByRole("combobox", { name: "Routing method" }).click();
  await page.getByRole("option", { name: "Scribe model", exact: true }).click();
  const model = settings.getByRole("combobox", {
    name: "Scribe model",
    exact: true,
  });
  await expect(model).toBeEnabled();
  await model.click();
  await expect(
    page.getByRole("option", { name: "local-legal · local/legal" }),
  ).toBeVisible();
  await page.getByRole("option", { name: "api-legal · api/legal" }).click();
  await settings
    .getByRole("textbox", {
      name: "Additional metadata fields, separated by commas",
    })
    .fill("record.metadata.title");
  await settings.getByRole("button", { name: "Apply changes" }).click();
  await expect(router.getByText("api-legal", { exact: true })).toBeVisible();
});

test("installed plugin blocks can be configured and stale versions are refused", async ({
  page,
}, testInfo) => {
  let stale = false;
  const capability = {
    id: "fixture.algorithm.count",
    pluginId: "fixture",
    pluginVersion: "1.0.0",
    origin: "PLUGIN",
    execution: "REGISTERED",
    kind: "ALGORITHM",
    targets: ["PIPELINE"],
    name: "Count records",
    description: "Count records in the incoming document.",
    stepType: "algorithm",
    parametersSchemaJson: JSON.stringify({
      type: "object",
      properties: {
        limit: { type: "integer", title: "Record limit", default: 2 },
      },
      required: ["limit"],
      additionalProperties: false,
    }),
    compatibility: { allowedModels: [], requires: [] },
  };
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        subject: "ui-user",
        tenant: "ui-tenant",
        displayName: "Alex",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        requiresPasskey: false,
      },
    }),
  );
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage(() => {}),
  );
  await page.route("**/graphql", (route) => {
    const body = route.request().postDataJSON();
    if (body.query.includes("pipelineCapabilities"))
      return route.fulfill({
        json: {
          data: {
            pipelineCapabilities: {
              capabilities: [
                { ...capability, pluginVersion: stale ? "2.0.0" : "1.0.0" },
              ],
            },
          },
        },
      });
    if (body.query.includes("pipelineComposition")) {
      const definition = JSON.parse(body.variables.definition);
      const step = definition.pipeline[0];
      expect(step.capability_id).toBe(capability.id);
      expect(step.capability_version).toBe("1.0.0");
      expect(step.params.limit).toBe(7);
      return route.fulfill({
        json: {
          data: {
            pipelineComposition: {
              definition: JSON.stringify(definition),
              blocks: 2,
              transfers: 0,
            },
          },
        },
      });
    }
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route("**/pipeline/new", (route) =>
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
  await page.goto("/pipeline/new");
  await page.getByRole("button", { name: "Add a block", exact: true }).click();
  await page.getByRole("menuitem", { name: /Installed plugins/ }).click();
  const picker = page.getByRole("dialog", { name: "Installed plugin blocks" });
  await picker
    .getByRole("combobox", { name: "Plugin block", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Count records · fixture 1.0.0", exact: true })
    .click();
  await picker.getByRole("button", { name: "Add block", exact: true }).click();
  const node = page.locator('[data-step-kind="algorithm"]');
  await node.getByRole("button", { name: "Configure block" }).click();
  const settings = page.getByRole("dialog", { name: "Configure block" });
  await settings.getByRole("combobox", { name: "Incoming block" }).click();
  await page.getByRole("option", { name: "input", exact: true }).click();
  await settings.getByRole("spinbutton", { name: "Record limit" }).fill("7");
  await expect(
    settings.getByRole("button", { name: "Apply changes" }),
  ).toBeEnabled();
  await page.screenshot({
    path: testInfo.outputPath("plugin-desktop.png"),
    animations: "disabled",
    fullPage: true,
  });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await settings.getByRole("button", { name: "Apply changes" }).click();
  await page
    .getByRole("button", { name: "Check pipeline", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Composition validated" }),
  ).toBeVisible();
  stale = true;
  await node.getByRole("button", { name: "Configure block" }).click();
  await expect(settings.getByRole("alert")).toContainText("unavailable");
  await expect(
    settings.getByRole("button", { name: "Apply changes" }),
  ).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("plugin-narrow.png"),
    animations: "disabled",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
