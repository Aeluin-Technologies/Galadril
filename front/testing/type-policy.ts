import type { Linter } from "eslint";

/** Disallow erasing type evidence at first-party TypeScript and Vue boundaries. */
export const typePolicy: Linter.RulesRecord = {
  "@typescript-eslint/ban-ts-comment": [
    "error",
    { "ts-expect-error": true, "ts-ignore": true, "ts-nocheck": true },
  ],
  "@typescript-eslint/no-explicit-any": "error",
  "@typescript-eslint/no-non-null-assertion": "error",
  "@typescript-eslint/no-unsafe-argument": "error",
  "@typescript-eslint/no-unsafe-assignment": "error",
  "@typescript-eslint/no-unsafe-call": "error",
  "@typescript-eslint/no-unsafe-member-access": "error",
  "@typescript-eslint/no-unsafe-return": "error",
  "@typescript-eslint/no-unsafe-type-assertion": "error",
};
