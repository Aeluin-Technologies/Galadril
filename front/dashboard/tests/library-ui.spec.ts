import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { record } from "../shared/records.ts";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import JSZip from "jszip";

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
  let invalidate: (() => void) | undefined;
  let stopped = 0;
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
  await page.routeWebSocket("**/graphql", (socket) => {
    socket.onMessage((message) => {
      const frame = record(JSON.parse(String(message)));
      const query = record(frame.payload).query;
      if (frame.type === "connection_init")
        socket.send(JSON.stringify({ type: "connection_ack" }));
      if (
        frame.type === "subscribe" &&
        typeof query === "string" &&
        query.includes("LibraryChanges")
      ) {
        invalidate = () =>
          socket.send(
            JSON.stringify({
              type: "next",
              id: frame.id,
              payload: { data: { pengoloLibraryChanges: true } },
            }),
          );
        invalidate();
      }
      if (frame.type === "complete" && String(frame.id).startsWith("library:"))
        stopped++;
    });
  });
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
  await expect.poll(() => typeof invalidate).toBe("function");
  const original = files.at(0);
  expect(original).toBeDefined();
  if (!original || !invalidate) throw new Error("Library fixture not ready");
  files.push({
    ...original,
    id: "f".repeat(32),
    data: { ...original.data, name: "Published by another session.pdf" },
  });
  invalidate();
  await expect(
    library.getByText("Published by another session.pdf", { exact: true }),
  ).toBeVisible();
  files = files.filter((file) => file.id !== "f".repeat(32));
  invalidate();
  await expect(
    library.getByText("Published by another session.pdf", { exact: true }),
  ).toHaveCount(0);
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
  const archiveDownload = await download;
  expect(archiveDownload.suggestedFilename()).toBe("Galadril-documents.zip");
  const archivePath = await archiveDownload.path();
  if (!archivePath) throw new Error("Missing downloaded archive");
  const archive = await JSZip.loadAsync(await readFile(archivePath));
  expect(Object.keys(archive.files)).toEqual([
    "1-Evidence 1.txt",
    "2-Evidence 2.txt",
  ]);
  expect(await archive.file("1-Evidence 1.txt")?.async("string")).toBe(
    "Evidence one",
  );
  expect(await archive.file("2-Evidence 2.txt")?.async("string")).toBe(
    "Evidence two",
  );
  await library
    .getByRole("button", { name: "Start chat", exact: true })
    .click();
  await expect(page).toHaveURL(/\/pengolo$/);
  await expect.poll(() => stopped).toBeGreaterThan(0);
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
  const advanced = editor
    .locator("details")
    .filter({ has: page.getByText("Advanced", { exact: true }) });
  await expect(
    editor.getByText("Specialist context", { exact: true }),
  ).toBeHidden();
  await editor.evaluate(async (element) => {
    await Promise.all(
      element
        .getAnimations({ subtree: true })
        .map((animation) => animation.finished),
    );
  });
  const initialPosition = await advanced.evaluate(
    (element) => (element as HTMLElement).offsetTop,
  );
  await editor
    .getByRole("button", { name: "Customize project appearance", exact: true })
    .click();
  expect(
    await advanced.evaluate((element) => (element as HTMLElement).offsetTop),
  ).toEqual(initialPosition);
  await editor
    .getByRole("textbox", { name: "Project name", exact: true })
    .click();
  await expect(
    editor.getByRole("button", { name: "Use Scale icon", exact: true }),
  ).toBeHidden();
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

test("sign Tectonic PDFs with the same encrypted account key", async ({
  page,
}, testInfo) => {
  test.skip(
    process.env.GALADRIL_SIGNING_E2E !== "1",
    "Requires cached Tectonic bundle and pdfsig",
  );
  const { assertSignedPdf, compileSigningPdf, signingTrace } =
    await import("./signing-fixture.ts");
  const { mkdir } = await import("node:fs/promises");
  const dir = testInfo.outputPath("signing");
  await mkdir(dir, { recursive: true });
  const variants = [true, false, false].map((visible, index) => ({
    custom: index === 2,
    bytes: compileSigningPdf(dir, visible),
    visible,
    id: String(index + 1).repeat(32),
    name: visible
      ? "With field.pdf"
      : index === 2
        ? "Custom field.pdf"
        : "Without field.pdf",
  }));
  const authenticator = await page.context().newCDPSession(page);
  await authenticator.send("WebAuthn.enable");
  await authenticator.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      ctap2Version: "ctap2_1",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      hasPrf: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  const password = "a separate strong signing password";
  let envelope: string | null = null;
  let enrollments = 0;
  let marksRead = 0;
  let firstCertificate: string | undefined;
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
    socket.onMessage((message) => {
      if (record(JSON.parse(String(message))).type === "connection_init")
        socket.send(JSON.stringify({ type: "connection_ack" }));
    }),
  );
  await page.route("**/signing-fixture/*", (route) => {
    const file = variants.find(({ id }) =>
      new URL(route.request().url()).pathname.endsWith(id),
    );
    if (!file) return route.abort();
    return route.fulfill({
      contentType: "application/pdf",
      body: route.request().method() === "HEAD" ? undefined : file.bytes,
    });
  });
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON();
    let data: Record<string, unknown> = {};
    if (query.includes("query Workspace")) {
      data = {
        pengoloIdentity: {
          version: "1",
          administrator: false,
          webSearchAvailable: false,
        },
        pengoloModels: { defaultModel: "local", models: [] },
        pengoloProjects: [],
        pengoloChats: { items: [], after: null },
      };
    } else if (query.includes("pengoloLibrary(")) {
      data = {
        pengoloLibrary: {
          after: null,
          items: variants.map((file) => ({
            id: file.id,
            ownerId: "owner",
            projectId: null,
            revision: 1,
            updatedAt: 1,
            expiresAt: null,
            data: {
              name: file.name,
              mime: "application/pdf",
              size: file.bytes.length,
              status: "ready",
              temporarySession: null,
              retrieval: { status: "ready", warnings: [] },
            },
          })),
        },
      };
    } else if (query.includes("pengoloDownload(")) {
      data = {
        pengoloDownload: { url: `/signing-fixture/${variables.input.id}` },
      };
    } else if (query.includes("createPengoloSigningVault(")) {
      expect(envelope).toBeNull();
      expect(typeof variables.envelope).toBe("string");
      expect(variables.envelope).not.toContain(password);
      envelope = variables.envelope;
      enrollments++;
      data = { createPengoloSigningVault: true };
    } else if (query.includes("pengoloSigningVault")) {
      data = { pengoloSigningVault: envelope };
    } else if (query.includes("pengoloHandwrittenSignatureTrace")) {
      marksRead++;
      data = { pengoloHandwrittenSignatureTrace: JSON.stringify(signingTrace) };
    } else if (query.includes("pengoloHandwrittenSignatures")) {
      data = {
        pengoloHandwrittenSignatures: [
          { id: "a".repeat(32), label: "My mark", createdAt: 1 },
        ],
      };
    }
    return route.fulfill({ json: { data } });
  });
  const shell = await page.request.get("/login");
  await page.route(
    /(?:\/pengolo(?:\/library)?|\/mcp\/sign\/[a-f0-9]{32})(?:\?.*)?$/,
    (route) =>
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
  const libraryUrl = new URL("/pengolo/library", shell.url());
  if (libraryUrl.hostname === "127.0.0.1") libraryUrl.hostname = "localhost";
  await page.goto(libraryUrl.href);
  for (const variant of variants) {
    if (variant.custom) {
      await page.addInitScript(() => {
        PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable =
          async () => false;
      });
      await page.goto(new URL(`/mcp/sign/${variant.id}`, libraryUrl).href);
    } else {
      await page
        .getByRole("button", { name: `Actions for ${variant.name}` })
        .click();
      await page
        .getByRole("menuitem", { name: "Sign PDF", exact: true })
        .click();
    }
    const dialog = page.getByRole("dialog", { name: "Sign PDF", exact: true });
    if (variant.custom) {
      const canvas = dialog.locator("canvas");
      const center = dialog.getByRole("button", {
        name: "Place at page center",
      });
      await expect(center).toBeEnabled({ timeout: 15000 });
      await canvas.scrollIntoViewIfNeeded();
      const rect = await canvas.boundingBox();
      if (!rect) throw new Error("Missing PDF page");
      await page.mouse.move(
        rect.x + rect.width * 0.18,
        rect.y + rect.height * 0.19,
      );
      await page.mouse.down();
      await page.mouse.move(
        rect.x + rect.width * 0.56,
        rect.y + rect.height * 0.28,
      );
      await page.mouse.up();
      await dialog
        .getByRole("checkbox", {
          name: "Add my name and signing date below the mark",
        })
        .check();
    }
    if (variant.visible)
      await expect(
        dialog.getByText("galadril:claimant", { exact: true }),
      ).toBeVisible();
    await dialog
      .getByRole("button", { name: "Continue to authentication" })
      .click();
    const sign = dialog.getByRole("button", {
      name:
        variant.visible || variant.custom ? "Sign PDF" : "Sign with passkey",
      exact: true,
    });
    await expect(sign).toBeDisabled();
    await dialog
      .getByRole("checkbox", {
        name: "I have reviewed this PDF and agree to sign this version.",
      })
      .check();
    if (variant.visible) {
      await dialog
        .getByLabel("Signing password", { exact: true })
        .fill(password);
      await dialog.getByLabel("Confirm signing password").fill(password);
      await dialog
        .getByRole("checkbox", {
          name: "Also enable unlock with a compatible passkey",
        })
        .check();
    } else if (variant.custom) {
      const input = dialog.getByLabel("Signing password", { exact: true });
      await input.fill("incorrect signing password");
      await sign.click();
      await expect(
        dialog.getByText("The signing key could not be unlocked."),
      ).toBeVisible();
      await input.fill(password);
    } else {
      await expect(
        dialog.getByLabel("Signing password", { exact: true }),
      ).toHaveCount(0);
    }
    await sign.click();
    const downloadButton = dialog.getByRole("link", {
      name: "Download signed PDF",
    });
    await expect(downloadButton).toBeVisible({ timeout: 20000 });
    const download = page.waitForEvent("download", { timeout: 20000 });
    await downloadButton.click();
    const output = await download;
    const path = join(dir, output.suggestedFilename());
    await output.saveAs(path);
    const certificate = await assertSignedPdf(
      path,
      variant.bytes,
      variant.visible || variant.custom,
      variant.custom ? { page: 0 } : undefined,
    );
    if (firstCertificate) expect(certificate).toBe(firstCertificate);
    else firstCertificate = certificate;
    await dialog.getByRole("button", { name: "Close dialog" }).click();
  }
  expect(enrollments).toBe(1);
  expect(marksRead).toBe(2);
});
