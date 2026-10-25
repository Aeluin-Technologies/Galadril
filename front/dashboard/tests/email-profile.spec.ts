import { test, expect } from "@playwright/test";

test.use({ channel: "chromium" });

function previewPdf(): Buffer {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    "<< /Length 50 >>\nstream\nBT /F1 12 Tf 20 100 Td (Authorized preview) Tj ET\nendstream",
  ];
  let pdf = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(pdf));
    pdf += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(pdf);
  pdf += `xref\n0 6\n0000000000 65535 f \n${offsets
    .slice(1)
    .map((offset) => String(offset).padStart(10, "0") + " 00000 n ")
    .join(
      "\n",
    )}\ntrailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(pdf);
}

test("email profile uses searchable languages and named skills with a safe HTML signature preview", async ({
  page,
}, testInfo) => {
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
  const config = {
    name: "Mailbox " + "a".repeat(64),
    address: "owner@example.org",
    imap_host: "imap.example.org",
    imap_port: 993,
    smtp_host: "smtp.example.org",
    smtp_port: 587,
    folders: ["INBOX"],
    enabled: true,
    ingest: false,
    poll_seconds: 120,
    initial_sync: "new",
  };
  let saved: Record<string, unknown> | undefined;
  const mailRequests: Array<{ before: number; query: string }> = [];
  const mail = (number: number) => ({
    id: number.toString(16).padStart(32, "0"),
    sequence: number,
    folder: "INBOX",
    label: "",
    ingested: false,
    message: {
      message_id: `<${number}@example.org>`,
      thread_id: `<${number}@example.org>`,
      sender: "sender@example.org",
      recipients: ["owner@example.org"],
      subject: `Message ${number}`,
      date: "2026-10-25",
      body: "Private message details",
      references: "",
      reply_allowed: true,
      attachments: [],
    },
  });
  await page.route("**/graphql", (route) => {
    const { query, variables } = route.request().postDataJSON();
    if (query.includes("pengoloOpenai"))
      return route.fulfill({
        json: { data: { pengoloOpenai: { enabled: false, accounts: [] } } },
      });
    if (query.includes("emailOperation")) {
      const request = JSON.parse(variables.request);
      if (request.operation === "tracking_status")
        return route.fulfill({
          json: { data: { emailOperation: '{"available":true}' } },
        });
      if (request.operation === "thread")
        return route.fulfill({
          json: {
            data: {
              emailOperation: JSON.stringify({
                thread_id: "<26@example.org>",
                indexed_complete: true,
                truncated: false,
                messages: [
                  {
                    root: "<26@example.org>",
                    message_id: "<26@example.org>",
                    parent: "",
                    subject: "Message 26",
                    date: "2026-10-25",
                    source_key: "email/source.json",
                    model: "fixture",
                    analysis: "Reviewed source",
                    number: 1,
                    reference: "<26@example.org>/1",
                    received_id: mail(26).id,
                    draft_id: null,
                    ingested: true,
                    status: "received",
                  },
                ],
              }),
            },
          },
        });
      if (request.operation === "messages") {
        mailRequests.push({
          before: request.before ?? 0,
          query: request.query ?? "",
        });
        const items = request.query
          ? [
              {
                ...mail(27),
                message: {
                  ...mail(27).message,
                  subject: "Needle older message",
                },
              },
            ]
          : request.before
            ? [mail(1)]
            : Array.from({ length: 25 }, (_, index) => mail(26 - index));
        return route.fulfill({
          json: {
            data: {
              emailOperation: JSON.stringify({
                items,
                next_before: request.query || request.before ? 0 : 2,
              }),
            },
          },
        });
      }
      if (request.operation === "message")
        return route.fulfill({
          json: { data: { emailOperation: JSON.stringify(mail(26).message) } },
        });
      if (request.operation === "drafts_page")
        return route.fulfill({
          json: {
            data: {
              emailOperation: JSON.stringify({
                items: [
                  {
                    id: "f".repeat(32),
                    revision: 1,
                    status: "sent",
                    delivery_id: "<sent@example.org>",
                    automatic: false,
                    content: {
                      track_opens: true,
                      to: ["recipient@example.org"],
                      cc: [],
                      subject: "Sent fixture",
                      body: "Reviewed email",
                      reply_to_message_id: "",
                      references: "",
                      attachments: [],
                    },
                  },
                ],
                next_before: 0,
              }),
            },
          },
        });
      if (request.operation === "opens")
        return route.fulfill({
          json: {
            data: {
              emailOperation: JSON.stringify({
                items: [
                  { sequence: 2, opened_at: 1792930010, ip: "2001:db8::7" },
                  { sequence: 1, opened_at: 1792930000, ip: "203.0.113.7" },
                ],
                next_before: 0,
              }),
            },
          },
        });
      const response =
        request.operation === "accounts"
          ? [
              {
                id: "a".repeat(32),
                revision: 1,
                config,
                rights: [
                  "view",
                  "read",
                  "use",
                  "edit",
                  "reveal",
                  "send",
                  "approve",
                ],
                has_credentials: true,
                sync_status: "pending",
                last_sync: 0,
                can_manage_access: true,
              },
            ]
          : request.operation === "providers"
            ? []
            : request.operation === "profile"
              ? null
              : request.operation === "messages"
                ? { items: [], next_before: 0 }
                : request.operation === "drafts"
                  ? []
                  : request.operation === "reveal"
                    ? { username: "private-login", auth: "password" }
                    : { ok: true };
      if (request.operation === "save_profile") saved = request.profile;
      return route.fulfill({
        json: { data: { emailOperation: JSON.stringify(response) } },
      });
    }
    if (query.includes("query EmailThreadPipelines"))
      return route.fulfill({
        json: {
          data: {
            pipelineExecutions: [
              {
                commandId: "matched",
                pipelineId: "authorized",
                step: "route",
                status: "completed",
                correlationId: "one",
                sourceEmailAccountId: "a".repeat(32),
                sourceEmailId: mail(26).id,
              },
              {
                commandId: "unrelated",
                pipelineId: "other",
                step: "Foreign source",
                status: "completed",
                correlationId: "two",
                sourceEmailAccountId: "b".repeat(32),
                sourceEmailId: mail(26).id,
              },
            ],
            pipelineLinkTargets: [
              { pipelineId: "authorized", name: "Correspondence review" },
            ],
          },
        },
      });
    if (query.includes("scribeModels"))
      return route.fulfill({
        json: {
          data: {
            scribeModels: { models: [{ alias: "local", model: "local" }] },
          },
        },
      });
    if (query.includes("pipelineCapabilities"))
      return route.fulfill({
        json: {
          data: {
            pipelineCapabilities: {
              capabilities: [
                {
                  id: "utils.skill.email",
                  kind: "SKILL",
                  targets: ["SCRIBE"],
                  name: "Email correspondence",
                  description: "Compose grounded replies for review.",
                },
              ],
            },
          },
        },
      });
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route(/\/settings\/(?:email|user)(?:\?.*)?$/, (route) =>
    route.request().isNavigationRequest()
      ? route.fulfill({ response: shell })
      : route.continue(),
  );
  await page.goto("/settings/email");
  const navigation = page.getByRole("navigation", {
    name: "User settings",
    exact: true,
  });
  const emailSection = navigation.getByRole("link", { name: /Email/ });
  const oauthSection = navigation.getByRole("link", { name: /OAuth/ });
  await expect(emailSection).toHaveAttribute("aria-current", "page");
  await expect(
    page.getByRole("heading", { name: "OpenAI connection", exact: true }),
  ).toHaveCount(0);
  await oauthSection.click();
  await expect(page).toHaveURL(/\/settings\/user#oauth$/);
  await expect(oauthSection).toHaveAttribute("aria-current", "page");
  await expect(
    page.getByRole("heading", { name: "OAuth", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Connect an email account", exact: true }),
  ).toHaveCount(0);
  await page.goBack();
  await expect(emailSection).toHaveAttribute("aria-current", "page");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(emailSection).toBeVisible();
  await expect(oauthSection).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await oauthSection.click();
  await expect(page).toHaveURL(/\/settings\/user#oauth$/);
  await expect(
    page.getByRole("heading", { name: "OAuth", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(oauthSection).toHaveAttribute("aria-current", "page");
  await emailSection.click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await expect(
    page.getByRole("heading", { name: "Scribe response profile", exact: true }),
  ).toHaveCount(0);
  const mailbox = page.getByRole("link", {
    name: /Mailbox.*owner@example.org/,
  });
  await expect(mailbox).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("email-account-list-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(mailbox).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("email-account-list-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await mailbox.click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const messagesCard = page.getByRole("region", {
    name: "Messages",
    exact: true,
  });
  const draftsCard = page.getByRole("region", {
    name: "Drafts and deliveries",
    exact: true,
  });
  await expect(
    messagesCard.getByRole("searchbox", {
      name: "Search collected messages",
      exact: true,
    }),
  ).toBeVisible();
  await expect(draftsCard).toBeVisible();
  const messagesBounds = await messagesCard.boundingBox();
  const draftsBounds = await draftsCard.boundingBox();
  expect(
    Math.abs((messagesBounds?.width ?? 0) - (draftsBounds?.width ?? 1)),
  ).toBeLessThan(2);
  expect(draftsBounds?.y).toBeGreaterThan(
    (messagesBounds?.y ?? 0) + (messagesBounds?.height ?? 0),
  );
  await expect(
    messagesCard.getByRole("button", { name: "Search", exact: true }),
  ).toHaveCount(0);
  const messageList = messagesCard.getByRole("region", {
    name: "Collected messages",
    exact: true,
  });
  await expect(
    messageList.getByRole("button", { name: /^Message / }),
  ).toHaveCount(25);
  await messageList.evaluate((element) => {
    element.scrollTop = element.scrollHeight;
  });
  await expect(
    messageList.getByRole("button", { name: /^Message / }),
  ).toHaveCount(26);
  expect(mailRequests.some((request) => request.before === 2)).toBe(true);
  const search = messagesCard.getByRole("searchbox", {
    name: "Search collected messages",
    exact: true,
  });
  await search.fill("Needle");
  await expect(
    messageList.getByRole("button", { name: /^Needle older message/ }),
  ).toBeVisible();
  await expect(
    messageList.getByRole("button", { name: /^Needle/ }),
  ).toHaveCount(1);
  await search.fill("");
  await messageList.getByRole("button", { name: /^Message 26 / }).click();
  const messageDialog = page.getByRole("dialog", {
    name: "Message 26",
    exact: true,
  });
  await expect(messageDialog).toContainText("Private message details");
  await messageDialog
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await expect(
    page.getByText("Private message details", { exact: true }),
  ).toHaveCount(0);
  await messageList
    .getByRole("button", { name: "View thread for Message 26", exact: true })
    .click();
  const thread = page.getByRole("dialog", {
    name: "Correspondence thread",
    exact: true,
  });
  await expect(thread).toContainText("<26@example.org>/1");
  await expect(
    thread
      .locator(".vue-flow__node")
      .filter({ hasText: "Correspondence review · route" }),
  ).toHaveCount(1);
  await expect(thread).not.toContainText("Foreign source");
  await thread.getByRole("button", { name: "Close", exact: true }).click();
  await page
    .getByRole("button", { name: "Opening signals", exact: true })
    .click();
  const openings = page.getByRole("dialog", {
    name: "Opening signals",
    exact: true,
  });
  await expect(openings).toContainText("203.0.113.7");
  await expect(openings).toContainText("2001:db8::7");
  await expect(openings.locator("time")).toHaveCount(2);
  await openings.getByRole("button", { name: "Close", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Scribe response profile", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Edit connection", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("navigation", { name: "Account sections", exact: true })
    .getByRole("link", { name: "Replies", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Scribe response profile", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "All email accounts", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Scribe response profile", exact: true }),
  ).toHaveCount(0);
  await mailbox.click();
  await page
    .getByRole("navigation", { name: "Account sections", exact: true })
    .getByRole("link", { name: "Replies", exact: true })
    .click();
  await expect(
    page.getByRole("textbox", { name: "Signature", exact: true }),
  ).toHaveValue("");
  await expect(
    page.getByLabel("Fallback model aliases", { exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Skill IDs", { exact: true })).toHaveCount(0);
  await page.getByRole("combobox", { name: "Language", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Search languages", exact: true })
    .fill("fran");
  await expect(
    page.getByRole("option", { name: "English", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("option", { name: "Français", exact: true }).click();
  await page.getByRole("combobox", { name: "Skills", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Search skills", exact: true })
    .fill("correspondence");
  await expect(
    page.getByRole("option", { name: /Email correspondence/ }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page
    .getByRole("textbox", { name: "Signature", exact: true })
    .fill(
      '<b>Regards</b><img src="https://tracker.example/x"><script>alert(1)</script>',
    );
  const preview = page.getByTitle("Signature preview", { exact: true });
  await expect(preview).toHaveAttribute("sandbox", "");
  await expect(preview).not.toHaveAttribute(
    "srcdoc",
    /tracker.example|<script|<img/,
  );
  await page
    .getByRole("button", { name: "Account actions", exact: true })
    .click();
  await page
    .getByRole("menuitem", { name: "Reveal credentials", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Reveal credentials",
    exact: true,
  });
  await expect(dialog.getByLabel("Username", { exact: true })).toHaveValue(
    "private-login",
  );
  await expect(
    dialog.getByLabel("Password or access token", { exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close", exact: true }).click();
  await page
    .getByRole("button", { name: "Save response profile", exact: true })
    .click();
  await expect.poll(() => saved?.language).toBe("French");
});

for (const senderCount of [1, 2]) {
  test(`email proposal chooses a sender only after user action and sends once (${senderCount} accounts)`, async ({
    page,
  }) => {
    const proposal = {
      type: "email_draft",
      id: "b".repeat(32),
      to: ["recipient@example.org"],
      cc: [],
      bcc: ["hidden@example.org"],
      subject: "Hello",
      body: "A reviewed draft",
    };
    const chatId = "c".repeat(32);
    const message = {
      id: "m",
      role: "assistant",
      text: "Prepared an email for review.",
      status: "completed",
      emailDrafts: [JSON.stringify(proposal)],
      fileIds: ["d".repeat(32)],
      generationId: "f".repeat(32),
      activityKinds: [],
      evidence: [{ resourceType: "pengolo_file", resourceId: "d".repeat(32) }],
    };
    const chat = {
      id: chatId,
      ownerId: "owner",
      projectId: null,
      expiresAt: null,
      revision: 1,
      updatedAt: 1,
      data: {
        title: "Email",
        pinned: false,
        activeGeneration: null,
        messages: { items: [message], after: null },
      },
    };
    let sendCount = 0,
      accountReads = 0;
    let draft: Record<string, unknown> | undefined;
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
    await page.route("**/fixture-preview*", (route) =>
      route.fulfill({
        body: previewPdf(),
        contentType: "application/pdf",
        headers: {
          "x-frame-options": "SAMEORIGIN",
          "content-security-policy":
            "frame-ancestors 'self'; base-uri 'none'; form-action 'none'",
        },
      }),
    );
    await page.route("**/graphql", (route) => {
      const { query, variables } = route.request().postDataJSON();
      if (query.includes("emailOperation")) {
        const request = JSON.parse(variables.request);
        let value: unknown = { ok: true };
        if (request.operation === "proposal_status")
          value = { status: draft?.status ?? "new" };
        if (request.operation === "tracking_status")
          value = { available: true };
        if (request.operation === "accounts") {
          accountReads++;
          value = Array.from({ length: senderCount }, (_, i) => ({
            id: String(i + 1).repeat(32),
            revision: 1,
            config: {
              name: "Mail",
              address: `sender${i + 1}@example.org`,
              imap_host: "imap.example.org",
              imap_port: 993,
              smtp_host: "smtp.example.org",
              smtp_port: 587,
              folders: ["INBOX"],
              enabled: true,
              ingest: false,
              poll_seconds: 120,
              initial_sync: "new",
            },
            rights: ["view", "read", "use", "approve", "send"],
            has_credentials: true,
            sync_status: "ok",
            last_sync: 0,
            can_manage_access: false,
          }));
        }
        if (request.operation === "draft") {
          if (!draft)
            return route.fulfill({
              json: { errors: [{ extensions: { code: "EMAIL_FAILED" } }] },
            });
          value = draft;
        }
        if (request.operation === "save_draft") {
          expect(request.content.bcc).toEqual(["hidden@example.org"]);
          draft = {
            id: proposal.id,
            revision: 1,
            status: "draft",
            content: request.content,
          };
          value = draft;
        }
        if (request.operation === "send") {
          sendCount++;
          if (draft) draft.status = "sent";
        }
        return route.fulfill({
          json: { data: { emailOperation: JSON.stringify(value) } },
        });
      }
      if (query.includes("pengoloDownload"))
        return route.fulfill({
          json: {
            data: {
              pengoloDownload: {
                url: "/fixture-preview",
                name: "Charte des examens et règles de délibération universitaires.pdf",
                mime: "application/pdf",
                size: 800,
              },
            },
          },
        });
      if (query.includes("query Agents"))
        return route.fulfill({
          json: {
            data: {
              pengoloAgents: [
                {
                  id: "e".repeat(32),
                  ownerId: "",
                  projectId: null,
                  revision: 1,
                  updatedAt: 0,
                  data: {
                    name: "Email correspondence",
                    mission: "Compose a reviewed email",
                    skillIds: ["utils.skill.email"],
                    readOnly: true,
                    pluginId: "utils",
                  },
                },
              ],
              scribeCapabilities: {
                capabilities: [
                  {
                    id: "utils.skill.email",
                    kind: "SKILL",
                    execution: "REGISTERED",
                    pluginId: "utils",
                    name: "Email correspondence",
                    description: "Compose a reviewed email",
                    compatibility: { allowedModels: [], requires: [] },
                  },
                ],
              },
            },
          },
        });
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
          pengoloProjects: [],
          pengoloChats: { items: [chat], after: null },
          activeChat: chat,
        };
      else if (query.includes("pengoloChat(")) data = { pengoloChat: chat };
      else if (query.includes("pengoloChats"))
        data = { pengoloChats: { items: [chat], after: null } };
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
    await page.route(`**/pengolo/${chatId}`, (route) =>
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
    await page.goto(`/pengolo/${chatId}`);
    await expect(
      page.getByRole("textbox", { name: "Subject", exact: true }),
    ).toHaveValue("Hello");
    await expect(page.getByText("Work completed", { exact: true })).toHaveCount(
      0,
    );
    await expect(
      page.getByText("View work and calculations", { exact: true }),
    ).toHaveCount(0);
    await expect(
      page
        .getByRole("button", { name: "Copy response", exact: true })
        .locator("svg"),
    ).toHaveCount(1);
    await expect(page.getByText("pengolo file", { exact: false })).toHaveCount(
      0,
    );
    await page
      .getByRole("button", {
        name: "Charte des examens et règles de délibération universitaires.pdf",
        exact: true,
      })
      .click();
    const preview = page.getByRole("dialog", {
      name: "Charte des examens et règles de délibération universitaires.pdf",
      exact: true,
    });
    await expect(preview.locator("iframe")).toHaveAttribute("src", /^blob:/);
    await expect
      .poll(() =>
        page.frames().some((frame) => frame.url().startsWith("blob:")),
      )
      .toBe(true);
    const width = await preview.evaluate(
      (element) => element.getBoundingClientRect().width,
    );
    expect(width).toBeGreaterThan(800);
    await expect(
      preview.getByRole("button", { name: "Download", exact: true }),
    ).toBeEnabled();
    await preview
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();
    await page
      .getByRole("checkbox", { name: "Track opening signals", exact: true })
      .check();
    await page
      .getByRole("button", { name: "Add files and tools", exact: true })
      .click();
    await page
      .getByRole("menuitem", { name: "Specialist agents", exact: true })
      .click();
    const picker = page.getByRole("dialog", {
      name: "Specialist agents",
      exact: true,
    });
    await expect(
      picker.getByRole("textbox", { name: "Name", exact: true }),
    ).toHaveCount(0);
    await picker
      .getByRole("combobox", { name: "Specialist agents", exact: true })
      .click();
    await page
      .getByRole("combobox", { name: "Search specialists", exact: true })
      .fill("correspondence");
    await page.getByRole("option", { name: /Email correspondence/ }).click();
    await page.keyboard.press("Escape");
    await picker
      .getByRole("button", { name: "Manage specialists", exact: true })
      .click();
    const manager = page.getByRole("dialog", {
      name: "Manage specialists",
      exact: true,
    });
    await expect(
      manager.getByRole("button", { name: "Edit", exact: true }),
    ).toHaveCount(0);
    await manager
      .getByRole("button", { name: "Close dialog", exact: true })
      .click();
    expect(accountReads).toBe(0);
    expect(sendCount).toBe(0);
    await page.getByRole("button", { name: "Send email", exact: true }).click();
    if (senderCount === 2) {
      const chooser = page.getByRole("dialog", {
        name: "Choose the sending account",
        exact: true,
      });
      await expect(chooser).toBeVisible();
      expect(sendCount).toBe(0);
      await chooser
        .getByRole("combobox", { name: "Sending account", exact: true })
        .click();
      await page
        .getByRole("option", { name: "sender2@example.org", exact: true })
        .click();
      await chooser
        .getByRole("button", { name: "Send email", exact: true })
        .click();
    }
    await expect(page.getByText("Email sent", { exact: true })).toBeVisible();
    expect(sendCount).toBe(1);
    expect((draft?.content as { track_opens?: boolean }).track_opens).toBe(
      true,
    );
    await page.reload();
    await expect(page.getByText("Email sent", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Sent", exact: true }),
    ).toBeDisabled();
    expect(sendCount).toBe(1);
  });
}
