import type { Reporter, TestCase, TestResult } from "@playwright/test/reporter";

/** Retains actionable diagnostics even when Bazel stops the entire browser suite. */
export default class FailureReporter implements Reporter {
  onTestEnd(test: TestCase, result: TestResult): void {
    if (result.status === test.expectedStatus) return;
    console.error(`[${result.status}] ${test.titlePath().join(" > ")}`);
    for (const error of result.errors) {
      console.error(error.stack ?? error.message ?? String(error.value));
    }
  }
}
