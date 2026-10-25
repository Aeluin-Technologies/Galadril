import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("personal instructions load, save and survive navigation without exposing an owner selector", async ({
  page,
}, testInfo) => {
  let preferences = { revision: 0, instructions: "" };
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
    const body = route.request().postDataJSON();
    if (body.query.includes("updatePengoloPreferences")) {
      expect(Object.keys(body.variables.input).sort()).toEqual([
        "instructions",
        "revision",
      ]);
      expect(body.variables.input.revision).toBe(preferences.revision);
      preferences = {
        revision: preferences.revision + 1,
        instructions: body.variables.input.instructions,
      };
      return route.fulfill({
        json: { data: { updatePengoloPreferences: preferences } },
      });
    }
    if (body.query.includes("pengoloPreferences"))
      return route.fulfill({
        json: { data: { pengoloPreferences: preferences } },
      });
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route(/\/settings\/user(?:\?.*)?$/, (route) =>
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
  await page.goto("/settings/user#personalization");
  const field = page.getByRole("textbox", {
    name: "Personal instructions",
    exact: true,
  });
  await expect(field).toBeEnabled();
  await field.fill("Réponds en français, avec un ton sobre.");
  await page
    .getByRole("button", { name: "Save instructions", exact: true })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Instructions saved" }),
  ).toBeVisible();
  await page.reload();
  await expect(field).toHaveValue("Réponds en français, avec un ton sobre.");
  await page.screenshot({
    path: testInfo.outputPath("personal-instructions-wide.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(field).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("personal-instructions-narrow.png"),
    fullPage: true,
  });
  await page.evaluate(() =>
    window.dispatchEvent(new Event("pengolo-access-invalidated")),
  );
  await expect(field).toHaveValue("");
  await expect(field).toBeDisabled();
});

test("email setup shows progress and skips detected servers without unavailable OAuth", async ({
  page,
}, testInfo) => {
  const requests: unknown[] = [];
  const creations: unknown[] = [];
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
    const input = route.request().postDataJSON();
    if (input.query.includes("emailOperation")) {
      const request = JSON.parse(input.variables.request);
      if (request.operation === "discover") {
        requests.push(request);
        expect(Object.keys(request).sort()).toEqual(["domain", "operation"]);
        const found =
          request.domain === "example.org" || request.domain === "gmail.com";
        return route.fulfill({
          json: {
            data: {
              emailOperation: JSON.stringify({
                domain: request.domain,
                oauth_provider:
                  request.domain === "gmail.com" ? "google" : null,
                imap: found
                  ? [
                      {
                        host: "imap.provider.org",
                        port: 993,
                        security: "tls",
                        priority: 0,
                        weight: 0,
                      },
                    ]
                  : [],
                smtp: found
                  ? [
                      {
                        host: "smtp.provider.org",
                        port: 587,
                        security: "starttls",
                        priority: 0,
                        weight: 0,
                      },
                    ]
                  : [],
                imap_status: found ? "found" : "absent",
                smtp_status: found ? "found" : "absent",
              }),
            },
          },
        });
      }
      if (request.operation === "validate_servers")
        return route.fulfill({
          json: { data: { emailOperation: '{"ok":true}' } },
        });
      if (request.operation === "create") {
        creations.push(request);
        return route.fulfill({
          json: { data: { emailOperation: '{"ok":true}' } },
        });
      }
      return route.fulfill({ json: { data: { emailOperation: "[]" } } });
    }
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route(/\/settings\/(?:email|user)(?:\?.*)?$/, (route) =>
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
  await page.goto("/settings/email");
  await page
    .getByRole("button", { name: "Connect an email account", exact: true })
    .first()
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Connect an email account",
    exact: true,
  });
  const address = dialog.getByRole("textbox", {
    name: "Email address",
    exact: true,
  });
  await expect(
    dialog.getByRole("textbox", { name: "IMAP server", exact: true }),
  ).toHaveCount(0);
  await expect(dialog.getByLabel(/Password or access token/)).toHaveCount(0);
  await expect(
    dialog.getByText("Step 1 of 3 · Email address", { exact: true }),
  ).toBeVisible();
  await address.fill("person@example.org");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByText("Everything was configured automatically.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(dialog.getByLabel(/Password or access token/)).toHaveValue("");
  await expect(
    dialog.getByRole("textbox", { name: "IMAP server", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await address.fill("person@missing.example");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByRole("textbox", { name: "IMAP server", exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("textbox", { name: "IMAP server", exact: true }),
  ).toHaveValue("");
  await expect(dialog.getByLabel(/Password or access token/)).toHaveCount(0);
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await address.fill("person@missing.example");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog
    .getByRole("textbox", { name: "IMAP server", exact: true })
    .fill("https://imap.example.org");
  await dialog
    .getByRole("textbox", { name: "SMTP server", exact: true })
    .fill("smtp.example.org");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByRole("textbox", { name: "IMAP server", exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("textbox", { name: "IMAP server", exact: true })
    .fill("imap.example.org");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByText("Step 3 of 3 · Sign in", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await expect(
    dialog.getByRole("textbox", { name: "IMAP server", exact: true }),
  ).toHaveValue("imap.example.org");
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await address.fill("person@gmail.com");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "Connect Gmail", exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByText(/OAuth for this provider has not been configured/),
  ).toHaveCount(0);
  await expect(
    dialog.getByText("Step 3 of 3 · Sign in", { exact: true }),
  ).toBeVisible();
  await expect(dialog.locator('[aria-current="step"]')).toContainText(
    "Sign in",
  );
  await expect(
    dialog.getByText("Everything was configured automatically.", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Edit manually", exact: true }),
  ).toBeVisible();
  await expect(dialog.getByLabel(/Password or access token/)).toHaveValue("");
  await expect(dialog.getByText(/Use a Google app password/)).toBeVisible();
  expect(requests).toHaveLength(4);
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("email-discovery-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("email-discovery-narrow.png"),
    fullPage: true,
  });
  await dialog
    .getByLabel(/Password or access token/)
    .fill("fixture-app-password");
  await dialog
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  expect(creations).toEqual([
    expect.objectContaining({
      operation: "create",
      config: expect.objectContaining({
        address: "person@gmail.com",
        initial_sync: "new",
        ingest: false,
      }),
      credentials: {
        username: "person@gmail.com",
        password: "fixture-app-password",
      },
    }),
  ]);
  await page
    .getByRole("button", { name: "Connect an email account", exact: true })
    .first()
    .click();
  await expect(address).toHaveValue("");
  await expect(dialog.getByLabel(/Password or access token/)).toHaveCount(0);
  await address.fill("sender@missing.example");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  const imap = dialog.getByRole("textbox", {
    name: "IMAP server",
    exact: true,
  });
  await expect(imap).not.toHaveAttribute("required");
  await expect(imap).toHaveValue("");
  await dialog
    .getByRole("textbox", { name: "SMTP server", exact: true })
    .fill("mail-eu.smtp2go.com");
  await dialog
    .getByRole("combobox", { name: "SMTP security", exact: true })
    .click();
  await page
    .getByRole("option", { name: "STARTTLS (2525)", exact: true })
    .click();
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(
    dialog.getByText(/This connection sends email only/),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Back", exact: true }).click();
  await expect(imap).toHaveValue("");
  await dialog.getByRole("button", { name: "Continue", exact: true }).click();
  await dialog
    .getByRole("textbox", { name: "Username", exact: true })
    .fill("smtp-user");
  await dialog
    .getByLabel(/Password or access token/)
    .fill("fixture-smtp-password");
  await dialog
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  expect(creations).toHaveLength(2);
  expect(creations[1]).toMatchObject({
    config: {
      imap_host: "",
      smtp_host: "mail-eu.smtp2go.com",
      smtp_port: 2525,
    },
    credentials: { username: "smtp-user", password: "fixture-smtp-password" },
  });
});

test("email editors rotate credentials without receiving existing secrets", async ({
  page,
}, testInfo) => {
  const account = {
    id: "a".repeat(32),
    revision: 1,
    rights: ["view", "read", "use", "edit", "delete"],
    config: {
      name: "Support",
      address: "support@example.org",
      imap_host: "imap.example.org",
      smtp_host: "smtp.example.org",
      imap_port: 993,
      smtp_port: 465,
      folders: ["INBOX"],
      enabled: true,
      ingest: false,
      poll_seconds: 120,
    },
    last_sync: 0,
    sync_status: "pending",
    has_credentials: true,
  };
  const operations: string[] = [];
  let savedProfile: unknown;
  const draft = {
    id: "d".repeat(32),
    revision: 1,
    status: "draft",
    delivery_id: "<delivery@example.org>",
    automatic: false,
    content: {
      to: ["alice@example.org"],
      cc: [],
      subject: "Delivery reply",
      body: "Original draft",
      reply_to_message_id: "",
      references: "",
      attachments: [],
    },
  };
  await page.route("**/auth/session", (route) =>
    route.fulfill({
      json: {
        subject: "editor",
        tenant: "tenant",
        displayName: "Editor",
        expiresAt: Math.floor(Date.now() / 1000) + 3600,
        requiresPasskey: false,
      },
    }),
  );
  await page.routeWebSocket("**/graphql", (socket) =>
    socket.onMessage(() => {}),
  );
  await page.route("**/graphql", (route) => {
    const input = route.request().postDataJSON();
    if (input.query.includes("emailOperation")) {
      const request = JSON.parse(input.variables.request);
      operations.push(request.operation);
      if (request.operation === "accounts")
        return route.fulfill({
          json: { data: { emailOperation: JSON.stringify([account]) } },
        });
      if (request.operation === "providers")
        return route.fulfill({ json: { data: { emailOperation: "[]" } } });
      if (request.operation === "profile")
        return route.fulfill({ json: { data: { emailOperation: "null" } } });
      if (request.operation === "save_profile") {
        savedProfile = request.profile;
        return route.fulfill({
          json: { data: { emailOperation: '{"ok":true}' } },
        });
      }
      if (request.operation === "drafts")
        return route.fulfill({
          json: { data: { emailOperation: JSON.stringify([draft]) } },
        });
      if (request.operation === "drafts_page")
        return route.fulfill({
          json: {
            data: {
              emailOperation: JSON.stringify({
                items: [draft],
                next_before: 0,
              }),
            },
          },
        });
      if (request.operation === "draft")
        return route.fulfill({
          json: { data: { emailOperation: JSON.stringify(draft) } },
        });
      if (request.operation === "update") {
        expect(request.credentials).toEqual({
          username: "replacement",
          password: "new-password",
        });
        account.revision++;
        return route.fulfill({
          json: { data: { emailOperation: JSON.stringify(account) } },
        });
      }
      return route.fulfill({ json: { data: { emailOperation: "[]" } } });
    }
    if (input.query.includes("scribeModels"))
      return route.fulfill({
        json: {
          data: {
            scribeModels: { models: [{ alias: "local", model: "local" }] },
          },
        },
      });
    if (input.query.includes("pipelineCapabilities"))
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
                  description: "Prepare grounded replies for review.",
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
  await page.addInitScript(() =>
    localStorage.setItem(
      "galadril_onboarding_state",
      JSON.stringify({ isTutorialCompleted: true }),
    ),
  );
  await page.goto("/settings/email");
  await expect(
    page.getByRole("heading", { name: "Email accounts", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("support@example.org", { exact: true }).first(),
  ).toBeVisible();
  await page.getByRole("link", { name: /support@example.org/ }).click();
  await page
    .getByRole("button", { name: "Account actions", exact: true })
    .click();
  await page
    .getByRole("menuitem", { name: "Sender and credentials", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Sender and credentials",
    exact: true,
  });
  await expect(dialog.getByLabel("Replacement password")).toHaveValue("");
  await expect(
    page.getByRole("button", { name: "Reveal credentials", exact: true }),
  ).toHaveCount(0);
  await dialog.getByLabel("Replacement username").fill("replacement");
  await dialog.getByLabel("Replacement password").fill("new-password");
  await dialog
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(dialog).not.toBeVisible();
  expect(operations).not.toContain("reveal");
  await page.getByRole("button", { name: "Edit draft", exact: true }).click();
  const composer = page.getByRole("dialog", {
    name: "Compose a draft",
    exact: true,
  });
  await expect(
    composer.getByRole("textbox", { name: "Message", exact: true }),
  ).toHaveValue("Original draft");
  await composer.getByRole("button", { name: "Cancel", exact: true }).click();
  account.rights.push("automate");
  await page
    .getByRole("link", { name: "All email accounts", exact: true })
    .click();
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByRole("link", { name: /support@example.org/ }).click();
  await page
    .getByRole("navigation", { name: "Account sections", exact: true })
    .getByRole("link", { name: "Replies", exact: true })
    .click();
  await expect(
    page.getByRole("combobox", { name: "Scribe model", exact: true }),
  ).toContainText("local");
  await page.getByRole("combobox", { name: "Reply mode", exact: true }).click();
  await page
    .getByRole("option", {
      name: "Automatic replies to allowed senders",
      exact: true,
    })
    .click();
  await page
    .getByLabel("Allowed sender addresses", { exact: true })
    .fill("alice@example.org");
  await page
    .getByLabel("Maximum automatic replies per sender in 24 hours", {
      exact: true,
    })
    .fill("3");
  await page
    .getByRole("checkbox", {
      name: "Send only one automatic acknowledgment per conversation",
      exact: true,
    })
    .check();
  await page
    .getByRole("button", { name: "Save response profile", exact: true })
    .click();
  await expect
    .poll(() => savedProfile)
    .toMatchObject({
      model: "local",
      skill_ids: ["utils.skill.email"],
      sender_daily_limit: 3,
      one_reply_per_thread: true,
      mode: "automatic",
    });
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  // Sandboxed user HTML has an opaque origin; its host remains explicitly checked.
  await expect(
    page.getByTitle("Signature preview", { exact: true }),
  ).toHaveAttribute("sandbox", "");
  expect(
    (
      await new AxeBuilder({ page })
        .exclude('iframe[title="Signature preview"]')
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("email-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("heading", { name: "Email accounts", exact: true }),
  ).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .exclude('iframe[title="Signature preview"]')
        .withTags(["wcag2a", "wcag2aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("email-narrow.png"),
    fullPage: true,
  });
});
