import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Operations overview" }),
  ).toBeVisible();
});

test("disabled and pending buttons keep their visual state on hover and press", async ({
  page,
}) => {
  await page.goto("/buttons");
  for (const name of [
    "primary disabled",
    "secondary disabled",
    "ghost disabled",
    "danger disabled",
    "Inherited disabled",
    "Pending action",
  ]) {
    const button = page.getByRole("button", { name, exact: true });
    const style = () =>
      button.evaluate((element) => {
        const css = getComputedStyle(element);
        return [css.backgroundColor, css.color, css.transform, css.boxShadow];
      });
    const original = await style();
    await button.hover({ force: true });
    await page.waitForTimeout(200);
    expect(await style()).toEqual(original);
    await page.mouse.down();
    await page.waitForTimeout(200);
    expect(await style()).toEqual(original);
    await page.mouse.up();
    await page.mouse.move(0, 0);
  }
});

test("tooltip arrows remain anchored to the trigger center at viewport edges", async ({
  page,
}) => {
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    for (const name of ["About this demonstration", "Rename workspace"]) {
      const trigger = page.getByRole("button", { name, exact: true });
      await trigger.focus();
      const tooltip = page.getByRole("tooltip", { name });
      await expect(tooltip).toBeVisible();
      const arrow = tooltip.locator("[data-ui=tooltip-arrow]");
      await expect(arrow).toBeVisible();
      const triggerElement = await trigger.elementHandle();
      await expect
        .poll(() =>
          arrow.evaluate((element, target) => {
            if (!target) throw new Error("Missing tooltip trigger");
            const pointer = element.getBoundingClientRect();
            const anchor = target.getBoundingClientRect();
            return Math.abs(
              pointer.x + pointer.width / 2 - anchor.x - anchor.width / 2,
            );
          }, triggerElement),
        )
        .toBeLessThan(1);
      await triggerElement?.dispose();
      const box = await tooltip.boundingBox();
      expect(box && box.x >= 7 && box.x + box.width <= width - 7).toBe(true);
      if (name === "Rename workspace")
        await expect(tooltip).toHaveAttribute("data-side", "top");
      await page.keyboard.press("Escape");
    }
  }
});

test("tooltip typography and width respond to viewport size while open", async ({
  page,
}) => {
  const trigger = page.getByRole("button", {
    name: "About this demonstration",
  });
  await trigger.focus();
  const tooltip = page.getByRole("tooltip", {
    name: "About this demonstration",
  });
  await expect(tooltip).toBeVisible();
  const desktopSize = await tooltip.evaluate((element) =>
    parseFloat(getComputedStyle(element).fontSize),
  );
  await page.setViewportSize({ width: 320, height: 844 });
  await expect
    .poll(() =>
      tooltip.evaluate((element) =>
        parseFloat(getComputedStyle(element).fontSize),
      ),
    )
    .toBeLessThan(desktopSize);
  expect(
    await tooltip.evaluate((element) =>
      parseFloat(getComputedStyle(element).fontSize),
    ),
  ).toBeGreaterThanOrEqual(12);
  const box = await tooltip.boundingBox();
  expect(box && box.width <= 304).toBe(true);
});

test("select popup uses shared styling and keyboard selection with focus restoration", async ({
  page,
}) => {
  const trigger = page.getByRole("combobox", { name: "Status" });
  await trigger.click();
  const list = page.getByRole("listbox");
  await expect(list).toBeVisible();
  await expect(list).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(list).toHaveCSS("border-radius", "12px");
  await expect(
    page.getByRole("option", { name: "All statuses" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("End");
  await expect(
    page.getByRole("option", { name: "Needs attention" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(trigger).toHaveText("Needs attention");
  await expect(trigger).toBeFocused();
  await expect(list).toBeHidden();
  await expect(
    page.getByRole("row", { name: /Customer directory/ }),
  ).toHaveCount(0);
  await trigger.press("ArrowDown");
  await expect(list).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await expect(list).toBeHidden();
});

test("select remains usable inside modal top layers with disabled options and typeahead", async ({
  page,
}) => {
  await page.goto("/controls");
  await page.getByRole("button", { name: "Choose a connection" }).click();
  const dialog = page.getByRole("dialog", { name: "Connection settings" });
  const trigger = dialog.getByRole("combobox", { name: "Connection type" });
  await expect(trigger).toHaveAccessibleDescription(
    "Choose an available connector.",
  );
  await expect(trigger).toHaveAttribute("aria-required", "true");
  await trigger.click();
  const list = dialog.getByRole("listbox");
  await expect(list).toBeVisible();
  await expect(page.getByRole("option", { name: "Database" })).toBeFocused();
  await expect(
    page.getByRole("option", { name: "Event stream" }),
  ).toHaveAttribute("aria-disabled", "true");
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.keyboard.press("o");
  await expect(
    page.getByRole("option", { name: "Object storage" }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(trigger).toHaveText("Object storage");
  await expect
    .poll(() =>
      dialog
        .locator("form")
        .evaluate((form) =>
          new FormData(form as HTMLFormElement).get("connection"),
        ),
    )
    .toBe("storage");
  await trigger.press("ArrowDown");
  await expect(list).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(list).toBeHidden();
  await expect(dialog).toBeVisible();
  await expect(trigger).toBeFocused();
});

test("select reflects a renamed option without changing its submitted identifier", async ({
  page,
}) => {
  await page.goto("/controls");
  await page.getByRole("button", { name: "Choose a connection" }).click();
  const trigger = page.getByRole("combobox", { name: "Connection type" });
  await trigger.click();
  await page.getByRole("option", { name: "Database", exact: true }).click();
  await page.getByRole("button", { name: "Rename connection" }).click();
  await expect(trigger).toHaveText("Analytics database");
  expect(
    await page
      .locator("dialog form")
      .evaluate((form) =>
        new FormData(form as HTMLFormElement).get("connection"),
      ),
  ).toBe("database");
});

test("shared form controls preserve external labels, numeric and boolean models, arrays and native form values", async ({
  page,
}) => {
  await page.goto("/form");
  await page.getByRole("spinbutton", { name: "Quantity" }).fill("7");
  await expect(page.getByLabel("Quantity value")).toHaveText("8");
  const access = page.getByRole("combobox", { name: "Access" });
  await expect(access).toHaveText("Personal");
  await access.click();
  await page.getByRole("option", { name: "Shared", exact: true }).click();
  await expect(page.getByLabel("Access value")).toHaveText("shared");
  await page
    .getByRole("textbox", { name: "Instructions" })
    .fill("Keep the context");
  await page.getByRole("checkbox", { name: "Analyst" }).check();
  await page.getByRole("checkbox", { name: "Operator" }).check();
  await page.getByRole("checkbox", { name: "Analyst" }).uncheck();
  await expect(page.getByLabel("Selected roles")).toHaveText("operator");
  expect(
    await page
      .locator("form")
      .evaluate((form) =>
        Object.fromEntries(new FormData(form as HTMLFormElement)),
      ),
  ).toMatchObject({ quantity: "7", access: "true", notes: "Keep the context" });
  expect(
    (await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze())
      .violations,
  ).toEqual([]);
});

test("composer text remains readable at desktop and mobile widths", async ({
  page,
}) => {
  await page.goto("/form");
  for (const width of [1440, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 });
    const composer = page.getByRole("textbox", {
      name: "Message",
      exact: true,
    });
    await expect(composer).toHaveCSS("font-size", "16px");
    await composer.fill("Keep this message readable");
    await expect(composer).toHaveValue("Keep this message readable");
  }
});

test("capture the shared workspace and its responsive and dialog states", async ({
  page,
}, info) => {
  await page.screenshot({
    path: info.outputPath("workspace-desktop.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("combobox", { name: "Status" }).click();
  await expect(
    page.getByRole("option", { name: "All statuses" }),
  ).toBeFocused();
  await page.screenshot({
    path: info.outputPath("workspace-dropdown.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "Rename workspace" }).focus();
  await expect(
    page.getByRole("tooltip", { name: "Rename workspace" }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("workspace-tooltip.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "Rename workspace" }).click();
  await expect(
    page.getByRole("dialog", { name: "Rename workspace" }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("workspace-dialog.png"),
    fullPage: true,
    animations: "disabled",
  });
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: info.outputPath("workspace-mobile.png"),
    fullPage: true,
    animations: "disabled",
  });
});

test("legacy adapters preserve click, disabled, attribute and modal slot contracts", async ({
  page,
}) => {
  await page.goto("/legacy");
  await page.getByTestId("plain-button").click();
  await expect(page.getByLabel("Click count")).toHaveText("1");
  await expect(
    page.getByRole("button", { name: "Unavailable" }),
  ).toBeDisabled();
  await expect(page.getByTestId("plain-button")).not.toHaveAttribute("title");
  const editor = page.getByRole("button", { name: "Edit workspace" });
  await editor.focus();
  const tooltip = page.getByRole("tooltip", {
    name: "Open the workspace editor",
  });
  await expect(tooltip).toBeVisible();
  await expect(tooltip).toHaveCSS("margin-top", "0px");
  await editor.click();
  await expect(page.getByText("Existing modal content")).toBeVisible();
  await page.getByRole("button", { name: "Finish editing" }).click();
  await expect(page.getByText("Existing modal content")).toBeHidden();
});

test("navigation aligns icon columns and select uses a centered decorative chevron", async ({
  page,
}) => {
  const navigation = page.getByRole("navigation", {
    name: "Workspace navigation",
  });
  const positions = await navigation
    .locator("button svg")
    .evaluateAll((icons) =>
      icons.map((icon) => Math.round(icon.getBoundingClientRect().left)),
    );
  expect(new Set(positions).size, JSON.stringify(positions)).toBe(1);
  const select = page.getByRole("combobox", { name: "Status" });
  await expect(select).toHaveCSS("appearance", "none");
  const arrow = select.locator("..").locator("svg");
  await expect(arrow).toHaveAttribute("aria-hidden", "true");
  const boxes = await Promise.all([select.boundingBox(), arrow.boundingBox()]);
  const control = boxes[0];
  const chevron = boxes[1];
  expect(
    control &&
      chevron &&
      Math.abs(control.y + control.height / 2 - chevron.y - chevron.height / 2),
  ).toBeLessThan(1);
});

test("Nuxt registers UI components and Tailwind emits semantic styles", async ({
  page,
}) => {
  const button = page.getByRole("button", {
    name: "Synchronize sources",
    exact: true,
  });
  await expect(button).toHaveCSS("background-color", "rgb(245, 158, 11)");
  await expect(button).toHaveCSS("color", "rgb(23, 33, 47)");
  await expect(button).toHaveCSS("border-radius", "8px");
  await expect(page.locator("[data-ui=card]").first()).toHaveCSS(
    "background-color",
    "rgb(255, 255, 255)",
  );
});

test("async actions block repeated clicks, announce results and release pending state", async ({
  page,
}) => {
  const button = page.getByRole("button", {
    name: "Synchronize sources",
    exact: true,
  });
  await button.click();
  await expect(button).toHaveAttribute("aria-busy", "true");
  await expect(button).toHaveAttribute("aria-disabled", "true");
  await expect(button.locator("[data-ui=spinner]")).toBeVisible();
  await expect(
    page.getByRole("status").filter({ hasText: "Operation in progress" }),
  ).toHaveCount(1);
  await button.dispatchEvent("click");
  await expect(
    page.getByRole("status").filter({ hasText: "Sources synchronized" }),
  ).toBeVisible();
  await expect(button).toHaveAttribute("aria-busy", "false");
  await expect(page.getByTestId("sync-count")).toHaveText("1");
  await expect(button).toBeFocused();
});

test("failed actions recover and report an accessible error", async ({
  page,
}) => {
  await page.getByText("Interaction examples", { exact: true }).click();
  const button = page.getByRole("button", { name: "Test an error" });
  await button.click();
  await expect(page.getByRole("alert")).toContainText(
    "The operation failed. Try again.",
  );
  await expect(button).toHaveAttribute("aria-busy", "false");
  await button.click();
  await expect(page.getByTestId("failure-count")).toHaveText("2");
});

test("icon tooltips support focus, Escape, hover and dialog top layers", async ({
  page,
}) => {
  const rename = page.getByRole("button", { name: "Rename workspace" });
  await rename.focus();
  let tip = page.getByRole("tooltip", { name: "Rename workspace" });
  await expect(tip).toBeVisible();
  await expect(rename).toHaveAccessibleName("Rename workspace");
  await expect(rename).toHaveAttribute(
    "aria-describedby",
    (await tip.getAttribute("id")) ?? "",
  );
  await page.keyboard.press("Escape");
  await expect(tip).toBeHidden();
  await rename.blur();
  await rename.hover();
  await expect(tip).toBeVisible();
  const bounds = await tip.boundingBox();
  if (!bounds) throw new Error("Missing tooltip bounds");
  await page.mouse.move(
    bounds.x + bounds.width / 2,
    bounds.y + bounds.height / 2,
  );
  await page.waitForTimeout(250);
  await expect(tip).toBeVisible();
  expect(
    await tip.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      return element.contains(
        document.elementFromPoint(
          rect.x + rect.width / 2,
          rect.y + rect.height / 2,
        ),
      );
    }),
  ).toBe(false);
  await rename.click();
  await expect(
    page.getByRole("dialog", { name: "Rename workspace" }),
  ).toBeVisible();
  const close = page.getByRole("button", { name: "Close dialog" });
  await close.focus();
  tip = page.getByRole("tooltip", { name: "Close dialog" });
  await expect(tip).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(tip).toBeHidden();
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("dialog traps focus, validates fields and restores the opener", async ({
  page,
}) => {
  const opener = page.getByRole("button", { name: "Rename workspace" });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Rename workspace" });
  const input = dialog.getByRole("textbox", { name: "Workspace name" });
  await expect(input).toBeFocused();
  await input.fill("");
  await dialog.getByRole("button", { name: "Save changes" }).click();
  await expect(input).toHaveAttribute("aria-invalid", "true");
  await expect(input).toHaveAccessibleDescription("Enter a workspace name.");
  await input.fill("European operations");
  await dialog.getByRole("button", { name: "Save changes" }).click();
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "European operations" }),
  ).toBeVisible();
  await opener.click();
  await dialog.getByRole("button", { name: "Save changes" }).focus();
  await page.keyboard.press("Tab");
  await expect(
    dialog.getByRole("button", { name: "Close dialog" }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("tooltip")).toBeHidden();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
});

test("labels, selection and checkbox state are accessible", async ({
  page,
}) => {
  await page
    .getByRole("searchbox", { name: "Search sources" })
    .fill("nothing matches");
  await expect(
    page.getByRole("heading", { name: "No matching sources" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(
    page.getByRole("searchbox", { name: "Search sources" }),
  ).toHaveValue("");
  await page.getByRole("combobox", { name: "Status" }).click();
  await page
    .getByRole("option", { name: "Needs attention", exact: true })
    .click();
  await expect(
    page.getByRole("row", { name: /Warehouse events/ }),
  ).toBeVisible();
  await expect(
    page.getByRole("row", { name: /Customer directory/ }),
  ).toHaveCount(0);
  const checkbox = page.getByRole("checkbox", {
    name: "Notify me about failures",
  });
  await checkbox.check();
  await expect(checkbox).toBeChecked();
});

test("keyboard focus, reduced motion, narrow layout and automated accessibility", async ({
  page,
}) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page
    .getByRole("button", { name: "Synchronize sources", exact: true })
    .click();
  await expect(page.locator("[data-ui=spinner]").first()).toHaveCSS(
    "animation-name",
    "none",
  );
  await expect(
    page.getByRole("status").filter({ hasText: "Sources synchronized" }),
  ).toBeVisible();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.getByRole("button", { name: "Rename workspace" }).click();
  expect(
    (
      await new AxeBuilder({ page })
        .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
        .analyze()
    ).violations,
  ).toEqual([]);
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("tooltip arrows stay centered when a trigger is flush with the viewport edge", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 740 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const trigger = page.getByRole("button", {
    name: "About this demonstration",
    exact: true,
  });
  await trigger.evaluate((element) => {
    Object.assign(element.style, {
      position: "fixed",
      right: "0px",
      top: "160px",
      width: "24px",
      minWidth: "24px",
    });
  });
  await trigger.focus();
  const tooltip = page.getByRole("tooltip", {
    name: "About this demonstration",
  });
  await expect(tooltip).toBeVisible();
  const target = await trigger.elementHandle();
  await expect
    .poll(() =>
      tooltip.locator("[data-ui=tooltip-arrow]").evaluate((element, button) => {
        if (!button) throw new Error("Missing trigger");
        const arrow = element.getBoundingClientRect();
        const anchor = button.getBoundingClientRect();
        return Math.abs(
          arrow.x + arrow.width / 2 - anchor.x - anchor.width / 2,
        );
      }, target),
    )
    .toBeLessThan(1);
  await target?.dispose();
});
