/** Exercises the shipped GOL editor against the real authenticated services. */
import { test, expect, type BrowserContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";

const ontologyId = "workspace.browser";
const displayName = "Browser ontology lifecycle";
const access = (symbol: string) => {
  const permissions = [
    { name: "read_access", relation: "read" },
    { name: "write_access", relation: "write" },
    { name: "describe", relation: "describe" },
    { name: "transfer", relation: "transfer" },
    { name: "execute", relation: "execute" },
  ]
    .map(
      ({ name, relation }) =>
        `permission ${name} { relation: "${relation}"; cedar: ${JSON.stringify(`Gol::Action::"${relation}"`)}; }`,
    )
    .join("\n");
  return `access ${symbol} { resource: "gol_resource"; ${permissions} }`;
};
const source = `ontology ${ontologyId} version "1.0.0" {
  entity BrowserMessage {
    field title: String;
    field hiddenRecipient: Option<String> @label(PrivateMail);
    field attachment: Option<Ref<BrowserAttachment>>;
  }
  entity BrowserAttachment { field name: String; }
  relation BrowserAttached {
    role message: BrowserMessage [1..1];
    role attachment: BrowserAttachment [1..1];
  }
  security label PrivateMail { classification: Confidential; compartments: [Mailbox]; }
  ${access("BrowserMessage")}
  ${access("BrowserAttachment")}
  ${access("BrowserAttached")}
}`;

function isObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function object(value: unknown): Record<string, unknown> {
  if (!isObject(value)) throw new Error("Expected a Gateway object");
  return value;
}
function text(value: unknown): string {
  if (typeof value !== "string" || !value)
    throw new Error("Expected a Gateway identifier");
  return value;
}
function cookies(value: unknown): Parameters<BrowserContext["addCookies"]>[0] {
  if (!Array.isArray(value))
    throw new Error("Expected fixture session cookies");
  return value.map((raw: unknown) => {
    const value = object(raw);
    const sameSite = value.sameSite;
    if (sameSite !== "Strict" && sameSite !== "Lax" && sameSite !== "None")
      throw new Error("Invalid session cookie policy");
    if (
      typeof value.httpOnly !== "boolean" ||
      typeof value.secure !== "boolean" ||
      typeof value.value !== "string"
    )
      throw new Error("Invalid session cookie flags");
    return {
      name: text(value.name),
      value: value.value,
      url: text(value.url),
      sameSite,
      httpOnly: value.httpOnly,
      secure: value.secure,
    };
  });
}
const fixture = object(JSON.parse(readFileSync("live-fixture.json", "utf8")));

async function graphql(
  context: BrowserContext,
  query: string,
  variables: Record<string, unknown> = {},
) {
  const response = await context.request.post("/graphql", {
    headers: { origin: "https://127.0.0.1:18080" },
    data: { query, variables },
  });
  expect(response.status()).toBe(200);
  const result = object(await response.json());
  expect(result.errors).toBeUndefined();
  return object(result.data);
}
async function mutationResult(
  page: Page,
  field: string,
  action: () => Promise<void>,
) {
  const pending = page.waitForResponse((response) => {
    if (
      response.request().method() !== "POST" ||
      new URL(response.url()).pathname !== "/graphql"
    )
      return false;
    const body = object(response.request().postDataJSON());
    return typeof body.query === "string" && body.query.includes(`${field}(`);
  });
  await action();
  const response = await pending;
  expect(response.status()).toBe(200);
  const value = object(await response.json());
  expect(value.errors).toBeUndefined();
  return object(object(value.data)[field]);
}
async function publish(page: Page) {
  await page.getByRole("button", { name: "Validate", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Publish", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Publish", exact: true }).click();
  return mutationResult(page, "publishGolOntology", () =>
    page
      .getByRole("dialog", { name: "Review publication", exact: true })
      .getByRole("button", { name: "Publish generation", exact: true })
      .click(),
  );
}
async function share(context: BrowserContext, enabled: boolean) {
  const result = await graphql(
    context,
    'mutation($id:String!,$member:String!,$enabled:Boolean!){administration{setResourceAccess(resourceType:"ontology",resourceId:$id,subjectType:"user",subjectId:$member,relation:"reader",enabled:$enabled)}}',
    { id: ontologyId, member: text(fixture.memberId), enabled },
  );
  expect(object(result.administration).setResourceAccess).toBe(true);
}
async function selectMessage(page: Page) {
  await page
    .getByRole("navigation", { name: "Authorized symbols", exact: true })
    .getByRole("button", { name: "BrowserMessage", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "BrowserMessage", exact: true }),
  ).toBeVisible();
}

test("GOL authoring, atomic generations, authorized schemas and warm-cache revocation", async ({
  page,
  context,
  browser,
}, testInfo) => {
  await context.addCookies(cookies(fixture.cookies));
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  const legacy: string[] = [];
  page.on("request", (request) => {
    if (
      request.method() !== "POST" ||
      new URL(request.url()).pathname !== "/graphql"
    )
      return;
    const query = object(request.postDataJSON()).query;
    if (
      typeof query === "string" &&
      /\b(?:ontologies|ontologyWorkspace|pluginOntologies|authorOntology|saveOntologyDraft|publishOntology)\b/.test(
        query,
      )
    )
      legacy.push("legacy ontology operation");
  });
  await page.goto("/ontology/new");
  await page
    .getByRole("textbox", { name: "Ontology identifier", exact: true })
    .fill(ontologyId);
  await page
    .getByRole("textbox", { name: "Ontology name", exact: true })
    .fill(displayName);
  const editor = page.getByRole("textbox", { name: "GOL source", exact: true });
  const invalidSource = source.replace(
    "field title: String;",
    "field title: ;",
  );
  await editor.fill(invalidSource);
  const invalidDraft = await mutationResult(page, "saveGolOntology", () =>
    page.getByRole("button", { name: "Save draft", exact: true }).click(),
  );
  expect(object(invalidDraft.summary).sourceRevision).toBeTruthy();
  const invalidValidation = await mutationResult(
    page,
    "validateGolOntology",
    () => page.getByRole("button", { name: "Validate", exact: true }).click(),
  );
  expect(invalidValidation.status).toBe("invalid");
  await expect(
    page.getByRole("region", { name: "Compiler diagnostics", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publish", exact: true }),
  ).toBeDisabled();
  await expect(editor).toHaveValue(invalidSource);
  await editor.fill(source);
  await mutationResult(page, "saveGolOntology", () =>
    page.getByRole("button", { name: "Save draft", exact: true }).click(),
  );
  const first = await publish(page);
  const firstGeneration = text(first.generation);
  await expect(page.getByText(firstGeneration, { exact: true })).toBeVisible();
  await share(context, true);

  const viewer = await browser.newContext({
    baseURL: "https://127.0.0.1:18080",
  });
  try {
    await viewer.addCookies(cookies(fixture.memberCookies));
    await viewer.addInitScript(() =>
      localStorage.setItem(
        "galadril_onboarding_state",
        JSON.stringify({ isTutorialCompleted: true }),
      ),
    );
    const memberPage = await viewer.newPage();
    let sourceReads = 0;
    const inspected: Promise<void>[] = [];
    let disclosedPrivateName = false;
    let inspectionFailures = 0;
    memberPage.on("response", (response) => {
      if (
        response.request().method() !== "POST" ||
        new URL(response.url()).pathname !== "/graphql"
      )
        return;
      inspected.push(
        response.text().then(
          (value) => {
            disclosedPrivateName ||= value.includes("hiddenRecipient");
          },
          () => {
            inspectionFailures++;
          },
        ),
      );
    });
    memberPage.on("request", (request) => {
      if (
        request.method() !== "POST" ||
        new URL(request.url()).pathname !== "/graphql"
      )
        return;
      const query = object(request.postDataJSON()).query;
      if (typeof query === "string" && query.includes("golOntologyWorkspace("))
        sourceReads++;
    });
    await memberPage.goto(`/ontology/edit/${ontologyId}`);
    await selectMessage(memberPage);
    await expect(
      memberPage.getByRole("button", { name: "Edit GOL", exact: true }),
    ).toHaveCount(0);
    await memberPage
      .getByRole("button", { name: "Preview JSON Schema", exact: true })
      .click();
    await expect(
      memberPage.getByRole("region", { name: "JSON Schema", exact: true }),
    ).toContainText('"title"');
    expect(sourceReads).toBe(0);
    await expect(memberPage.locator("body")).not.toContainText(
      "hiddenRecipient",
    );
    await Promise.all(inspected);
    expect(inspectionFailures).toBe(0);
    expect(disclosedPrivateName).toBe(false);
    await memberPage.screenshot({
      path: testInfo.outputPath("gol-live-desktop.png"),
      fullPage: true,
    });
    await memberPage.setViewportSize({ width: 390, height: 844 });
    expect(
      await memberPage.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await memberPage.screenshot({
      path: testInfo.outputPath("gol-live-mobile.png"),
      fullPage: true,
    });

    await editor.fill(
      source.replace(
        "field title: String;",
        "field title: String;\n    field archived: bool;",
      ),
    );
    await mutationResult(page, "saveGolOntology", () =>
      page.getByRole("button", { name: "Save draft", exact: true }).click(),
    );
    const second = await publish(page);
    expect(second.generation).not.toBe(firstGeneration);
    const pinned = await graphql(
      viewer,
      "query($id:String!,$generation:String){golOntologySymbols(ontologyId:$id,generation:$generation){generation symbols{key name properties{name}}}}",
      { id: ontologyId, generation: firstGeneration },
    );
    const pinnedProjection = object(pinned.golOntologySymbols);
    expect(pinnedProjection.generation).toBe(firstGeneration);
    expect(JSON.stringify(pinnedProjection)).not.toContain("archived");
    await memberPage.reload();
    await selectMessage(memberPage);
    await expect(
      memberPage.getByRole("cell", { name: "archived", exact: true }),
    ).toBeVisible();
    await memberPage
      .getByRole("button", { name: "Preview JSON Schema", exact: true })
      .click();
    await expect(
      memberPage.getByRole("region", { name: "JSON Schema", exact: true }),
    ).toBeVisible();
    await share(context, false);
    await memberPage
      .getByRole("button", { name: "Preview JSON Schema", exact: true })
      .click();
    await expect(memberPage.getByRole("alert")).toBeVisible();
    await expect(
      memberPage.getByRole("heading", { name: "BrowserMessage", exact: true }),
    ).toHaveCount(0);
    await expect(
      memberPage.getByRole("heading", { name: displayName, exact: true }),
    ).toHaveCount(0);
    await expect(
      memberPage.getByRole("region", { name: "JSON Schema", exact: true }),
    ).toHaveCount(0);
    expect(sourceReads).toBe(0);
    await Promise.all(inspected);
    expect(inspectionFailures).toBe(0);
    expect(disclosedPrivateName).toBe(false);
    const denied = await viewer.request.post("/graphql", {
      headers: { origin: "https://127.0.0.1:18080" },
      data: {
        query:
          "query($id:String!){golOntologyWorkspace(ontologyId:$id){files{path text}}}",
        variables: { id: ontologyId },
      },
    });
    const denial = object(await denied.json());
    expect(denial.errors).toBeTruthy();
    expect(JSON.stringify(denial)).not.toContain("BrowserMessage");
    await page.goto(`/ontology/edit/${ontologyId}`);
    await page.getByRole("button", { name: "Edit GOL", exact: true }).click();
    await page
      .getByRole("button", { name: "Delete ontology", exact: true })
      .click();
    await mutationResult(page, "deleteGolOntology", () =>
      page
        .getByRole("dialog", { name: "Delete ontology", exact: true })
        .getByRole("button", { name: "Delete ontology", exact: true })
        .click(),
    );
    await expect(page).toHaveURL(/\/ontology$/);
    await expect(
      page.getByRole("link", { name: displayName, exact: true }),
    ).toHaveCount(0);
    expect(legacy).toEqual([]);
  } finally {
    await viewer.close();
  }
});
