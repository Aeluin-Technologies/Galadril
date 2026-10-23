import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { AuthenticationBudget } from "./authentication-budget.ts";

function budgetTest(run) {
  const directory = mkdtempSync(join(tmpdir(), "galadril-auth-budget-"));
  try {
    run(join(directory, "budget.json"));
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

test("a replacement worker retains the shared source IP budget", () =>
  budgetTest((path) => {
    const budget = new AuthenticationBudget(path);
    assert.equal(budget.delay(1000), 0);
    budget.observe({
      "x-ratelimit-reset": "60000",
      "x-ratelimit-remaining": "5",
    });
    assert.equal(new AuthenticationBudget(path).delay(1000), 59100);
    assert.equal(new AuthenticationBudget(path).delay(60101), 0);
  }));

test("delayed and malformed headers cannot replenish a known window", () =>
  budgetTest((path) => {
    const budget = new AuthenticationBudget(path);
    budget.observe({
      "x-ratelimit-reset": "60000",
      "x-ratelimit-remaining": "4",
    });
    for (const headers of [
      {},
      { "x-ratelimit-reset": "60000", "x-ratelimit-remaining": "40" },
      { "x-ratelimit-reset": "50000", "x-ratelimit-remaining": "100" },
      { "x-ratelimit-reset": "60000", "x-ratelimit-remaining": "-1" },
      { "x-ratelimit-reset": "60000", "x-ratelimit-remaining": "NaN" },
    ])
      budget.observe(headers);
    assert.equal(new AuthenticationBudget(path).delay(1000), 59100);
    budget.observe({
      "x-ratelimit-reset": "120000",
      "x-ratelimit-remaining": "59",
    });
    assert.equal(new AuthenticationBudget(path).delay(60001), 0);
  }));

test("an implausible reset deadline fails rather than hanging the suite", () =>
  budgetTest((path) => {
    const budget = new AuthenticationBudget(path);
    budget.observe({
      "x-ratelimit-reset": "120000",
      "x-ratelimit-remaining": "0",
    });
    assert.throws(() => budget.delay(1000), /one minute/);
  }));
