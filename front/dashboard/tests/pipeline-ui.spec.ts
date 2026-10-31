import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import type { AuthoringState } from "../app/utils/pipeline.ts";

test("drafts preserve incomplete work and separate review, publication and deployment", async ({
  page,
}) => {
  let revision = "a".repeat(32),
    sequence = 1;
  let lifecycle = "draft",
    published: string | null = null;
  let deleted = false,
    conflict = false;
  let definition = {
    name: "Correspondence",
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
  };
  const authoring: AuthoringState = {
    branches: {},
    proposals: {},
    approval: null,
    release: null,
  };
  const artifact = () => ({
    pipeline_id: "mail",
    name: definition.name,
    head_revision_id: revision,
    published_revision_id: published,
    lifecycle,
    branch_names: ["main"],
    definition: JSON.stringify(definition),
    authoring: JSON.stringify(authoring),
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
    const { query, variables } = route.request().postDataJSON();
    if (query.includes("pipelineDiagnostics"))
      return route.fulfill({ json: { data: { pipelineDiagnostics: "[]" } } });
    if (query.includes("authorPipeline")) {
      expect(variables.expected).toBe(revision);
      const command = JSON.parse(variables.command);
      if (conflict)
        return route.fulfill({
          json: { data: null, errors: [{ extensions: { code: "CONFLICT" } }] },
        });
      if (command.operation === "save_draft") {
        definition = JSON.parse(variables.definition);
        lifecycle = "draft";
        authoring.approval = null;
      }
      if (command.operation === "create_branch")
        authoring.branches[command.branch] = {
          definition,
          base_definition: definition,
          base_revision_id: revision,
          author_id: "author",
          updated_at_ms: 1,
        };
      if (command.operation === "propose_merge")
        authoring.proposals[command.source_branch] = {
          source_branch: command.source_branch,
          source_hash: "reviewed",
          target_hash: "reviewed",
          author_id: "author",
          created_at_ms: 1,
        };
      if (command.operation === "merge")
        authoring.proposals = Object.fromEntries(
          Object.entries(authoring.proposals).filter(
            ([id]) => id !== command.proposal_id,
          ),
        );
      if (command.operation === "approve") {
        lifecycle = "approved";
        authoring.approval = {
          definition_hash: "reviewed",
          author_id: "author",
          reviewed_at_ms: 1,
        };
      }
      if (command.operation === "publish") {
        lifecycle = "published";
        authoring.release = {
          revision_id: revision,
          definition_hash: "reviewed",
          author_id: "author",
          published_at_ms: 1,
        };
      }
      if (command.operation === "deploy") {
        lifecycle = "deployed";
        const release = authoring.release;
        if (
          release &&
          typeof release === "object" &&
          "revision_id" in release &&
          typeof release.revision_id === "string"
        )
          published = release.revision_id;
      }
      revision = (++sequence).toString(16).padStart(32, "0");
      return route.fulfill({ json: { data: { authorPipeline: artifact() } } });
    }
    if (query.includes("pipelineDiagnostics"))
      return route.fulfill({
        json: {
          data: {
            pipelineDiagnostics: JSON.stringify(
              definition.pipeline.some((step) => !step.input_from.length)
                ? [{ code: "input", node_id: "filter" }]
                : [],
            ),
          },
        },
      });
    if (query.includes("pipelineComposition"))
      return route.fulfill({
        json: {
          data: {
            pipelineComposition: {
              definition: JSON.stringify(definition),
              blocks: definition.sources.length + definition.pipeline.length,
              transfers: 0,
            },
          },
        },
      });
    if (query.includes("deletePipeline")) {
      expect(variables.expected).toBe(revision);
      deleted = true;
      return route.fulfill({ json: { data: { deletePipeline: true } } });
    }
    if (query.includes("pipelineDefinitions"))
      return route.fulfill({
        json: { data: { pipelineDefinitions: deleted ? [] : [artifact()] } },
      });
    if (query.includes("rebasePreview")) {
      conflict = false;
      return route.fulfill({
        json: {
          data: {
            pipelineDefinition: {
              ...artifact(),
              rebasePreview: JSON.stringify({
                definition: JSON.parse(variables.definition),
                conflicts: [],
              }),
            },
          },
        },
      });
    }
    if (query.includes("mergePreview"))
      return route.fulfill({
        json: {
          data: {
            pipelineDefinition: {
              ...artifact(),
              mergePreview: JSON.stringify({ definition, conflicts: [] }),
            },
          },
        },
      });
    if (query.includes("history(after:"))
      return route.fulfill({
        json: {
          data: {
            pipelineDefinition: {
              history: {
                items: [
                  {
                    revision_id: revision,
                    name: definition.name,
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
    if (query.includes("comparison(revisionId:"))
      return route.fulfill({
        json: { data: { pipelineDefinition: { comparison: "[]" } } },
      });
    if (query.includes("pipelineDefinition("))
      return route.fulfill({
        json: { data: { pipelineDefinition: artifact() } },
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
  await page
    .getByRole("button", { name: "Rename pipeline", exact: true })
    .click();
  const field = page.getByRole("textbox", {
    name: "Pipeline Name",
    exact: true,
  });
  await expect(field).toHaveValue("Correspondence");
  await field.fill("Mail analysis");
  await page.getByRole("button", { name: "Add a block", exact: true }).click();
  await page.getByRole("menuitem", { name: /Filter records/ }).click();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Publish pipeline", exact: true }),
  ).toBeDisabled();
  expect(
    definition.pipeline.some(
      (step) => step.type === "filter" && step.input_from.length === 0,
    ),
  ).toBe(true);
  const incomplete = page.locator('[data-step-kind="filter"]');
  await incomplete.getByRole("button", { name: "Delete", exact: true }).click();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await page
    .getByRole("button", { name: "Publish pipeline", exact: true })
    .click();
  expect(published).toBeNull();
  await page
    .getByRole("button", { name: "Deploy published revision", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Deployed", exact: true }),
  ).toBeDisabled();
  expect(published).not.toBeNull();
  await page
    .getByRole("tab", { name: "Proposals & branches", exact: true })
    .click();
  await page
    .getByRole("textbox", { name: "New branch name", exact: true })
    .fill("review");
  await page
    .getByRole("button", { name: "Create branch", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Propose merge into main", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Review merge", exact: true })
    .last()
    .click();
  await page
    .getByRole("button", { name: "Approve and merge into main", exact: true })
    .click();
  await page.getByRole("tab", { name: "History", exact: true }).click();
  await expect(page.getByText("Saved revision", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Compare revision", exact: true })
    .click();
  await expect(
    page.getByText("These definitions are identical.", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Editor", exact: true }).click();
  conflict = true;
  await page
    .getByRole("button", { name: "Rename pipeline", exact: true })
    .click();
  await field.fill("Keep this draft");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Another author");
  await expect(
    page.getByRole("heading", { name: "Keep this draft", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Compare with latest revision", exact: true })
    .click();
  await page
    .getByRole("button", {
      name: "Apply reviewed result to editor",
      exact: true,
    })
    .click();
  await expect(
    page.getByRole("heading", { name: "Keep this draft", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save draft", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("button", { name: "Revision actions", exact: true })
    .click();
  await page
    .getByRole("menuitem", { name: "Delete pipeline", exact: true })
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
  let composedRevision: string | undefined;
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
    if (body.query.includes("pipelineDiagnostics"))
      return route.fulfill({ json: { data: { pipelineDiagnostics: "[]" } } });
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
      composedRevision = authored.pipeline.find(
        (step: { type: string }) => step.type === "pipeline",
      )?.pipeline_ref?.revision_id;
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
  const settings = page.getByRole("region", { name: "Configure block" });
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
  await expect(
    page.getByRole("button", { name: "Check pipeline", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("status").filter({ hasText: "Composition validated" }),
  ).toBeVisible();
  await expect.poll(() => composedRevision).toBe(revision);
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
  await settings
    .getByRole("combobox", { name: "Rule configuration", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Advanced routing", exact: true })
    .click();
  await settings
    .getByRole("combobox", { name: "Routing method", exact: true })
    .last()
    .click();
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
  await settings
    .getByRole("combobox", { name: "Routing method", exact: true })
    .last()
    .click();
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
  let composedStep: Record<string, unknown> | undefined;
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
    if (body.query.includes("pipelineDiagnostics"))
      return route.fulfill({ json: { data: { pipelineDiagnostics: "[]" } } });
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
      composedStep = definition.pipeline[0];
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
  await picker.getByRole("combobox", { name: "Plugin", exact: true }).click();
  await page.getByRole("option", { name: "fixture", exact: true }).click();
  await picker
    .getByRole("combobox", { name: "Plugin block", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Count records · fixture 1.0.0", exact: true })
    .click();
  await picker.getByRole("button", { name: "Add block", exact: true }).click();
  const node = page.locator('[data-step-kind="algorithm"]');
  await node.getByRole("button", { name: "Configure block" }).click();
  const settings = page.getByRole("region", { name: "Configure block" });
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
  await expect(
    page.getByRole("button", { name: "Check pipeline", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("status").filter({ hasText: "Composition validated" }),
  ).toBeVisible();
  await expect
    .poll(() => composedStep)
    .toMatchObject({
      capability_id: capability.id,
      capability_version: "1.0.0",
      params: { limit: 7 },
    });
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

test("email sources, business filters and template replies use readable plugin operations", async ({
  page,
}, testInfo) => {
  const common = {
    pluginId: "utils",
    pluginVersion: "1.0.0",
    origin: "PLUGIN",
    execution: "REGISTERED",
    targets: ["PIPELINE"],
    description:
      "Use authorized mailbox messages. Credentials stay in email settings.",
    compatibility: { allowedModels: [], requires: [] },
  };
  const capabilities = [
    {
      ...common,
      id: "utils.input.email",
      name: "Read incoming emails",
      kind: "INPUT",
      stepType: "source",
      parametersSchemaJson: JSON.stringify({
        type: "object",
        properties: {
          topic: { type: "string", default: "raw", "x-advanced": true },
          parser: { type: "string", default: "json", const: "json" },
          source_kind: { type: "string", default: "email", const: "email" },
          schema_path: {
            type: "string",
            default: "schemas/avro/email.avsc",
            const: "schemas/avro/email.avsc",
          },
          match_pattern: { type: "string", default: ".*", "x-advanced": true },
        },
      }),
    },
    {
      ...common,
      id: "utils.algorithm.compose_reply",
      name: "Compose email reply",
      kind: "ALGORITHM",
      stepType: "algorithm",
      parametersSchemaJson: JSON.stringify({
        type: "object",
        properties: {
          generation: {
            type: "string",
            title: "Generation method",
            enum: ["template", "scribe"],
            default: "template",
          },
          template: {
            type: "string",
            title: "Reply template",
            format: "multiline",
            default: "Hello {{ sender }}",
          },
          model_alias: {
            type: "string",
            title: "Scribe model",
            format: "model-alias",
          },
        },
      }),
    },
    {
      ...common,
      id: "utils.output.reply",
      name: "Send email reply",
      kind: "OUTPUT",
      stepType: "sink",
      parametersSchemaJson: JSON.stringify({ type: "object", properties: {} }),
    },
  ];
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
  let saved:
    | {
        pipeline_id: string;
        name: string;
        head_revision_id: string;
        published_revision_id: null;
        definition: string;
        authoring: string;
        lifecycle: string;
      }
    | undefined;
  await page.route("**/graphql", (route) => {
    const body = route.request().postDataJSON();
    if (body.query.includes("pipelineCapabilities"))
      return route.fulfill({
        json: { data: { pipelineCapabilities: { capabilities } } },
      });
    if (body.query.includes("scribeModels"))
      return route.fulfill({
        json: {
          data: {
            scribeModels: {
              models: [{ alias: "local", model: "local/model" }],
            },
          },
        },
      });
    if (body.query.includes("authorPipeline")) {
      saved = {
        pipeline_id: body.variables.id,
        name: body.variables.name,
        head_revision_id: "a".repeat(32),
        published_revision_id: null,
        definition: body.variables.definition,
        authoring: JSON.stringify({
          branches: {},
          proposals: {},
          approval: null,
          release: null,
        }),
        lifecycle: "draft",
      };
      return route.fulfill({ json: { data: { authorPipeline: saved } } });
    }
    if (body.query.includes("pipelineDefinition("))
      return route.fulfill({ json: { data: { pipelineDefinition: saved } } });
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route(/\/pipeline\/[^/?]+$/, (route) =>
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
  async function add(operation: string) {
    await page
      .getByRole("button", { name: "Add a block", exact: true })
      .click();
    await page.getByRole("menuitem", { name: /Installed plugins/ }).click();
    const picker = page.getByRole("dialog", {
      name: "Installed plugin blocks",
    });
    await picker.getByRole("combobox", { name: "Plugin", exact: true }).click();
    await page.getByRole("option", { name: "utils", exact: true }).click();
    await picker
      .getByRole("combobox", { name: "Plugin block", exact: true })
      .click();
    await page
      .getByRole("option", { name: `${operation} · utils 1.0.0`, exact: true })
      .click();
    await picker
      .getByRole("button", { name: "Add block", exact: true })
      .click();
  }
  await add("Read incoming emails");
  await page
    .locator('[data-pipeline-node="input"]')
    .getByRole("button", { name: "Delete", exact: true })
    .click();
  await page.getByRole("button", { name: "Add a block", exact: true }).click();
  await page.getByRole("menuitem", { name: /Filter records/ }).click();
  const filter = page.locator('[data-step-kind="filter"]');
  await filter
    .getByRole("button", { name: "Configure block", exact: true })
    .click();
  const inspector = page.getByRole("region", {
    name: "Configure block",
    exact: true,
  });
  await inspector
    .getByRole("combobox", { name: "Incoming block", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Read incoming emails", exact: true })
    .click();
  await inspector.getByRole("combobox", { name: "Field", exact: true }).click();
  await page.getByRole("option", { name: "Recipient", exact: true }).click();
  await inspector
    .getByRole("combobox", { name: "Operator", exact: true })
    .click();
  await page.getByRole("option", { name: "is equal to", exact: true }).click();
  await inspector
    .getByRole("textbox", { name: "Value", exact: true })
    .fill("help@example.org");
  await inspector
    .getByRole("button", { name: "Add condition", exact: true })
    .click();
  await inspector
    .getByRole("textbox", { name: "Value", exact: true })
    .last()
    .fill("Invoice");
  await inspector
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await add("Compose email reply");
  const compose = page.locator('[data-step-kind="algorithm"]');
  await compose
    .getByRole("button", { name: "Configure block", exact: true })
    .click();
  await inspector
    .getByRole("combobox", { name: "Incoming block", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Filter records", exact: true })
    .click();
  await inspector
    .getByRole("textbox", { name: "Reply template", exact: true })
    .fill("Hello {{ sender }}, about {{ subject }}.");
  await inspector
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await add("Send email reply");
  const output = page.locator('[data-step-kind="sink"]');
  await output
    .getByRole("button", { name: "Configure block", exact: true })
    .click();
  await inspector
    .getByRole("combobox", { name: "Incoming block", exact: true })
    .click();
  await page
    .getByRole("option", { name: "Compose email reply", exact: true })
    .click();
  await inspector
    .getByRole("button", { name: "Apply changes", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Arrange graph", exact: true })
    .click();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page).toHaveURL(/\/pipeline\/pipeline-/);
  await expect(page.locator('[data-step-kind="filter"]')).toBeVisible();
  expect(saved).toBeDefined();
  const definition = JSON.parse(saved?.definition ?? "{}");
  expect(
    definition.pipeline.find((step: { type: string }) => step.type === "filter")
      .params.conditions,
  ).toEqual([
    { field: "recipient", operator: "equals", value: "help@example.org" },
    { field: "subject", operator: "contains", value: "Invoice" },
  ]);
  expect(
    definition.pipeline.find(
      (step: { capability_id?: string }) =>
        step.capability_id === "utils.algorithm.compose_reply",
    ).params.template,
  ).toBe("Hello {{ sender }}, about {{ subject }}.");
  expect(Object.keys(definition.editor.positions)).toHaveLength(4);
  await page.screenshot({
    path: testInfo.outputPath("email-pipeline-desktop.png"),
    fullPage: true,
  });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
});

test("existing pipelines complete onboarding without retaining the create action", async ({
  page,
}) => {
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
  await page.route("**/graphql", (route) =>
    route.fulfill({
      json: {
        data: {
          pipelineDefinitions: [
            {
              pipeline_id: "existing",
              name: "Mail",
              head_revision_id: "a".repeat(32),
              published_revision_id: null,
              lifecycle: "draft",
              branch_names: ["main"],
            },
          ],
        },
      },
    }),
  );
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const shell = await page.request.get("/login");
  await page.route(/\/$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/");
  const first = page
    .locator("div")
    .filter({
      has: page.getByRole("heading", {
        name: "Processing Pipelines",
        exact: true,
      }),
    })
    .filter({ hasText: "01" })
    .last();
  await expect(
    page.getByRole("heading", { name: "Processing Pipelines", exact: true }),
  ).toBeVisible({ timeout: 60000 });
  await expect(page.locator('a[href="/pipeline/new"]')).toHaveCount(0);
  await expect(first.getByText("Validated", { exact: true })).toBeVisible();
});
