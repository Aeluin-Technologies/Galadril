import assert from "node:assert/strict";

/** Missing fixture data must fail the test rather than bypass strict null checking. */
export function required<T>(value: T | null | undefined): T {
  assert.ok(
    value !== null && value !== undefined,
    "Required fixture value is missing",
  );
  return value;
}
