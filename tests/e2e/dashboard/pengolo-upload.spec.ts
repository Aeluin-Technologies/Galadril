import { test, expect } from "@playwright/test";

test("Pengolo file drops announce their destination and preserve upload scope", async ({
  page,
}, testInfo) => {
  const projectId = "c".repeat(32);
  const inputs: Array<Record<string, unknown>> = [];
  let sent = 0,
    denied = false;
  const files = new Map<string, ReturnType<typeof file>>();
  function file(id: string, input: Record<string, unknown>) {
    return {
      id,
      ownerId: "owner",
      projectId: input.projectId ?? null,
      revision: 1,
      updatedAt: 1,
      expiresAt: Date.now() + 86400000,
      data: {
        name: String(input.name),
        mime: String(input.mime),
        size: Number(input.size),
        status: "ready",
        temporarySession: input.temporarySession ?? null,
        indexing: null,
        retrieval: { status: "ready", profileId: null, warnings: [] },
      },
    };
  }
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
  await page.route("**/fixture-drop/*", (route) =>
    route.fulfill({ status: 200, body: "" }),
  );
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON() as {
      query: string;
      variables: { input?: Record<string, unknown>; project?: string | null };
    };
    let data: Record<string, unknown> = {};
    if (query.includes("query Workspace"))
      data = {
        pengoloIdentity: {
          version: "drops",
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
        pengoloProjects: [
          {
            id: projectId,
            ownerId: "owner",
            projectId: null,
            revision: 1,
            updatedAt: 1,
            expiresAt: null,
            data: {
              name: "Research",
              icon: "folder",
              color: "#2563eb",
              shared: false,
              roles: [],
              contextMode: "project",
            },
          },
        ],
        pengoloChats: { items: [], after: null },
      };
    else if (query.includes("beginPengoloUpload")) {
      if (denied)
        return route.fulfill({
          json: {
            errors: [
              {
                message: "Private backend details",
                extensions: { code: "FORBIDDEN" },
              },
            ],
          },
        });
      const input = variables.input ?? {};
      inputs.push(input);
      const id = inputs.length.toString(16).padStart(32, "0");
      const record = file(id, input);
      files.set(id, record);
      data = {
        beginPengoloUpload: {
          file: record,
          url: `/fixture-drop/${id}`,
          headers: [],
        },
      };
    } else if (query.includes("finishPengoloUpload"))
      data = { finishPengoloUpload: files.get(String(variables.input?.id)) };
    else if (query.includes("pengoloLibrary"))
      data = {
        pengoloLibrary: {
          items: Array.from(files.values()).filter(
            (record) =>
              record.projectId === (variables.project ?? null) &&
              !record.data.temporarySession,
          ),
          after: null,
        },
      };
    else if (query.includes("sendPengolo")) {
      sent++;
      data = {};
    } else if (query.includes("pengoloChats"))
      data = { pengoloChats: { items: [], after: null } };
    else if (query.includes("pengoloAgents")) data = { pengoloAgents: [] };
    else if (query.includes("pengoloOpenai"))
      data = { pengoloOpenai: { enabled: false, accounts: [] } };
    else if (query.includes("closePengoloTemporary"))
      data = { closePengoloTemporary: true };
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
  const transfer = (names: string[]) =>
    page.evaluateHandle((names) => {
      const data = new DataTransfer();
      names.forEach((name) =>
        data.items.add(
          new File(["Drag and drop fixture"], name, { type: "text/plain" }),
        ),
      );
      return data;
    }, names);
  await page.goto("/pengolo");
  await expect(
    page.getByRole("combobox", { name: "Model", exact: true }),
  ).toBeEnabled();
  const zone = page.locator(".p-workspace"),
    overlay = page.getByRole("status", {
      name: "Drop files to attach them to this message",
      exact: true,
    });
  const dropped = await transfer(["Dropped evidence.txt"]);
  await zone.dispatchEvent("dragenter", { dataTransfer: dropped });
  await expect(overlay).toBeVisible();
  await page
    .locator(".p-conversation")
    .dispatchEvent("dragenter", { dataTransfer: dropped });
  await page
    .locator(".p-conversation")
    .dispatchEvent("dragleave", { dataTransfer: dropped });
  await expect(overlay).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("pengolo-drop-feedback.png"),
    fullPage: true,
  });
  await zone.dispatchEvent("drop", { dataTransfer: dropped });
  await expect(overlay).toHaveCount(0);
  await expect(page.locator(".p-file-chips")).toContainText(
    "Dropped evidence.txt",
  );
  expect(inputs[0]?.projectId).toBeNull();
  expect(inputs[0]?.temporarySession).toBeNull();
  expect(sent).toBe(0);
  const text = await page.evaluateHandle(() => {
    const data = new DataTransfer();
    data.setData("text/plain", "Ordinary text");
    return data;
  });
  await zone.dispatchEvent("dragenter", { dataTransfer: text });
  await expect(overlay).toHaveCount(0);
  await page
    .getByRole("button", { name: "Temporary chat", exact: true })
    .click();
  const temporary = await transfer(["Temporary evidence.txt"]);
  await zone.dispatchEvent("dragenter", { dataTransfer: temporary });
  await zone.dispatchEvent("drop", { dataTransfer: temporary });
  await expect(page.locator(".p-file-chips")).toContainText(
    "Temporary evidence.txt",
  );
  expect(inputs[1]?.temporarySession).toMatch(/^[a-f0-9]{32}$/);
  expect(inputs[1]?.projectId).toBeNull();
  await page.goto(`/pengolo/library?project=${projectId}`);
  const library = page.getByRole("region", {
    name: "File library",
    exact: true,
  });
  await expect(library).toBeVisible();
  await expect(library.getByRole("button",{name:"Upload files",exact:true})).toBeEnabled();
  const libraryDrop = await transfer(["Project evidence.txt"]);
  await zone.dispatchEvent("dragenter", { dataTransfer: libraryDrop });
  await expect(
    page.getByRole("status", {
      name: "Drop files to upload to Research",
      exact: true,
    }),
  ).toBeVisible();
  await zone.dispatchEvent("drop", { dataTransfer: libraryDrop });
  await expect(
    library.getByText("Project evidence.txt", { exact: true }),
  ).toBeVisible();
  expect(inputs[2]?.projectId).toBe(projectId);
  expect(inputs[2]?.temporarySession).toBeNull();
  denied = true;
  const rejected = await transfer(["Rejected evidence.txt"]);
  await zone.dispatchEvent("drop", { dataTransfer: rejected });
  await expect(page.getByRole("alert")).toContainText("Upload failed");
  await expect(
    page.getByText("Private backend details", { exact: true }),
  ).toHaveCount(0);
  await expect(
    library.getByText("Rejected evidence.txt", { exact: true }),
  ).toHaveCount(0);
  expect(sent).toBe(0);
});
