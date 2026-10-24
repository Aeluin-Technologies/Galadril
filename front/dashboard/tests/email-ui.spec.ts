import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

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
    return route.fulfill({ json: { data: {} } });
  });
  const shell = await page.request.get("/login");
  await page.route("**/settings/email", (route) =>
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
  await page
    .getByRole("button", { name: "Edit connection", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Edit connection",
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
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
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
      sender_daily_limit: 3,
      one_reply_per_thread: true,
      mode: "automatic",
    });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
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
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("email-narrow.png"),
    fullPage: true,
  });
});
