import { test as base } from "@playwright/test";
import { dirname, join } from "node:path";

export const test = base.extend<{ browserCoverage: void }>({
  browserCoverage: [
    async ({ page, baseURL }, use) => {
      const { COVERAGE_DIR, TEST_SRCDIR, TEST_WORKSPACE } = process.env;
      if (
        !COVERAGE_DIR ||
        !TEST_SRCDIR ||
        !TEST_WORKSPACE ||
        !baseURL ||
        process.env.GALADRIL_UI_TEST_ORIGIN
      ) {
        await use();
        return;
      }

      await page.coverage.startJSCoverage({ resetOnNavigation: false });
      try {
        await use();
      } finally {
        const profile = await page.coverage.stopJSCoverage();
        const { writeBrowserProfile } = await import("./browser-profile.ts");
        await writeBrowserProfile(
          page,
          profile,
          new URL(baseURL).origin,
          join(TEST_SRCDIR, TEST_WORKSPACE),
          dirname(process.cwd()),
          COVERAGE_DIR,
          process.env.GALADRIL_BROWSER_ASSETS,
          process.env.TEST_UNDECLARED_OUTPUTS_DIR ?? COVERAGE_DIR,
        );
      }
    },
    { auto: true },
  ],
});
