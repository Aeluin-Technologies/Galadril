import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/** These fixtures verify UI contracts; interservice E2E runs use the real Gateway. */
async function fixture(page: Page, author = true, listed = true) {
  const saveRevisions: string[] = [];
  const state = {
    saveRevisions,
    sourceReads: 0,
    saves: 0,
    publications: 0,
    revoked: false,
    source:
      'ontology communication.mail version "1.0.0" {\n  entity Message { field title: String; }\n}',
    revision: "source-1",
    generation: "generation-1",
  };
  const summary = () => ({
    ontologyId: "communication.mail",
    displayName: "Mail",
    sourceRevision: state.revision,
    activeGeneration: state.generation,
    activeSourceRevision: "source-1",
    status: "published",
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
    const query: string = body.query;
    const variables: Record<string, unknown> = body.variables ?? {};
    if (query.includes("golOntologyCanManage"))
      return route.fulfill({
        json: { data: { golOntologyCanManage: author && !state.revoked } },
      });
    if (state.revoked && query.includes("gol"))
      return route.fulfill({ json: { errors: [{ message: "Unavailable" }] } });
    if (query.includes("golOntologies"))
      return route.fulfill({
        json: { data: { golOntologies: listed ? [summary()] : [] } },
      });
    if (query.includes("golOntologyWorkspace")) {
      state.sourceReads++;
      expect(author).toBe(true);
      return route.fulfill({
        json: {
          data: {
            golOntologyWorkspace: {
              summary: summary(),
              files: [{ path: "mail.gol", text: state.source }],
              diagnostics: [],
              dependencies: [],
            },
          },
        },
      });
    }
    if (query.includes("saveGolOntology")) {
      saveRevisions.push(
        typeof variables.expectedRevision === "string"
          ? variables.expectedRevision
          : "",
      );
      if (variables.expectedRevision !== state.revision)
        return route.fulfill({
          json: { errors: [{ message: "Revision changed" }] },
        });
      expect(variables.files).toEqual([
        { path: "mail.gol", text: expect.any(String) },
      ]);
      const files = variables.files;
      if (!Array.isArray(files) || typeof files[0]?.text !== "string")
        throw new Error("Expected GOL files");
      state.source = files[0].text;
      state.revision = `source-${++state.saves + 1}`;
      return route.fulfill({
        json: {
          data: {
            saveGolOntology: {
              summary: summary(),
              files,
              diagnostics: [],
              dependencies: [],
            },
          },
        },
      });
    }
    if (query.includes("validateGolOntology"))
      return route.fulfill({
        json: {
          data: {
            validateGolOntology: {
              ontologyId: "communication.mail",
              sourceRevision: state.revision,
              status: state.source.includes("broken") ? "invalid" : "valid",
              diagnostics: state.source.includes("broken")
                ? [
                    {
                      code: "GOL_SYNTAX",
                      message: "Expected declaration",
                      path: "mail.gol",
                      start: 0,
                      end: 6,
                      severity: "error",
                    },
                  ]
                : [],
              dependencies: [],
            },
          },
        },
      });
    if (query.includes("publishGolOntology")) {
      expect(variables.sourceRevision).toBe(state.revision);
      expect(variables.expectedGeneration).toBe(state.generation);
      state.generation = `generation-${++state.publications + 1}`;
      return route.fulfill({
        json: {
          data: {
            publishGolOntology: {
              ontologyId: "communication.mail",
              sourceRevision: state.revision,
              generation: state.generation,
            },
          },
        },
      });
    }
    if (query.includes("golOntologySymbols"))
      return route.fulfill({
        json: {
          data: {
            golOntologySymbols: {
              generation: state.generation,
              symbols: [
                {
                  key: "communication.mail::Message@1.0.0",
                  name: "Message",
                  kind: "entity",
                  references: [],
                  properties: [
                    { key: "Message.title", name: "title", typeName: "String" },
                  ],
                },
              ],
            },
          },
        },
      });
    if (query.includes("golOntologySchema")) {
      expect(variables.generation).toBe(state.generation);
      expect(variables.fields).toEqual(["title"]);
      return route.fulfill({
        json: {
          data: {
            golOntologySchema: {
              type: "object",
              properties: { title: { type: "string" } },
              additionalProperties: false,
            },
          },
        },
      });
    }
    expect(query).not.toMatch(
      /pluginOntologies|ontologyWorkspace|saveOntologyDraft|publishOntology/,
    );
    return route.fulfill({ json: { data: {} } });
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
  return state;
}

test("GOL source validation precedes publishing and retains invalid drafts", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/ontology/edit/communication.mail");
  await page.getByRole("button", { name: "Edit GOL", exact: true }).click();
  const editor = page.getByRole("textbox", { name: "GOL source", exact: true });
  await editor.fill("broken");
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect.poll(() => state.saves).toBe(1);
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(
    page.getByText("Expected declaration", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publish", exact: true }),
  ).toBeDisabled();
  await expect(editor).toHaveValue("broken");
  await editor.fill('ontology communication.mail version "1.0.0" {}');
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Publish", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Publish generation", exact: true })
    .click();
  await expect.poll(() => state.publications).toBe(1);
  await expect(page.getByText("generation-2", { exact: true })).toBeVisible();
});

test("read-only exploration never fetches source and previews authorized schemas", async ({
  page,
}, testInfo) => {
  const state = await fixture(page, false);
  await page.goto("/ontology/edit/communication.mail");
  await expect(
    page.getByRole("heading", { name: "Message", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Edit GOL", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Preview JSON Schema", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "JSON Schema", exact: true }),
  ).toContainText('"title"');
  await expect(page.locator("body")).not.toContainText("confidential_secret");
  expect(state.sourceReads).toBe(0);
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("gol-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("gol-mobile.png"),
    fullPage: true,
  });
});

test("revocation clears cached metadata and schema previews", async ({
  page,
}) => {
  const state = await fixture(page, false);
  await page.goto("/ontology/edit/communication.mail");
  await page
    .getByRole("button", { name: "Preview JSON Schema", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "JSON Schema", exact: true }),
  ).toBeVisible();
  state.revoked = true;
  await page.evaluate(() =>
    window.dispatchEvent(new Event("pengolo-access-invalidated")),
  );
  await expect(
    page.getByRole("heading", { name: "Message", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "JSON Schema", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Mail", exact: true }),
  ).toHaveCount(0);
  expect(state.sourceReads).toBe(0);
});

test("a denied refresh removes a previously exposed projection without a manual invalidation event", async ({
  page,
}) => {
  const state = await fixture(page, false);
  await page.goto("/ontology/edit/communication.mail");
  const preview = page.getByRole("button", {
    name: "Preview JSON Schema",
    exact: true,
  });
  await preview.click();
  await expect(
    page.getByRole("region", { name: "JSON Schema", exact: true }),
  ).toBeVisible();
  state.revoked = true;
  await preview.click();
  await expect(
    page.getByRole("heading", { name: "Message", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "JSON Schema", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole("alert")).toBeVisible();
});

test("an authorized direct ontology link works beyond the bounded catalog without fetching source", async ({
  page,
}) => {
  const state = await fixture(page, false, false);
  await page.goto("/ontology/edit/communication.mail");
  await expect(
    page.getByRole("heading", { name: "Message", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("generation-1", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "communication.mail", exact: true }),
  ).toBeVisible();
  expect(state.sourceReads).toBe(0);
});

test("retrying a source conflict preserves the draft and its original comparison revision", async ({
  page,
}) => {
  const state = await fixture(page);
  await page.goto("/ontology/edit/communication.mail");
  await page.getByRole("button", { name: "Edit GOL", exact: true }).click();
  const editor = page.getByRole("textbox", { name: "GOL source", exact: true });
  await editor.fill("own unfinished draft");
  state.revision = "source-concurrent";
  state.source = "concurrently saved source";
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Save draft", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect.poll(() => state.saveRevisions.length).toBe(2);
  expect(state.saveRevisions).toEqual(["source-1", "source-1"]);
  expect(state.source).toBe("concurrently saved source");
  await expect(editor).toHaveValue("own unfinished draft");
  await page
    .getByRole("button", { name: "Review latest sources", exact: true })
    .click();
  const comparison = page.getByRole("dialog", {
    name: "Review latest sources",
    exact: true,
  });
  await expect(comparison).toContainText("concurrently saved source");
  await expect(comparison).toContainText("own unfinished draft");
  await comparison
    .getByRole("button", { name: "Keep my draft", exact: true })
    .click();
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await expect.poll(() => state.saves).toBe(1);
  expect(state.saveRevisions).toEqual([
    "source-1",
    "source-1",
    "source-concurrent",
  ]);
  expect(state.source).toBe("own unfinished draft");
});
