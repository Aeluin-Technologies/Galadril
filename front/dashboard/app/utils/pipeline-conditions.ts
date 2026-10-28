import type { Json } from "./pipeline";

export type BusinessField =
  "sender" | "recipient" | "subject" | "body" | "account";
export type ConditionOperator =
  "equals" | "contains" | "starts_with" | "not_equals";
export interface BusinessCondition {
  field: BusinessField;
  operator: ConditionOperator;
  value: string;
}
export const businessFields: readonly BusinessField[] = [
  "sender",
  "recipient",
  "subject",
  "body",
  "account",
];
export const conditionOperators: readonly ConditionOperator[] = [
  "equals",
  "contains",
  "starts_with",
  "not_equals",
];

/** Runtime paths remain an internal contract; authored rules use a bounded vocabulary. */
export function conditionParams(
  mode: "route" | "filter",
  conditions: readonly BusinessCondition[],
): Record<string, Json> {
  if (
    !conditions.length ||
    conditions.length > 16 ||
    conditions.some(
      (rule) =>
        !businessFields.includes(rule.field) ||
        !conditionOperators.includes(rule.operator) ||
        !rule.value.trim() ||
        rule.value.length > 2048,
    )
  )
    throw new Error("CONDITION_INVALID");
  return {
    mode,
    conditions: conditions.map((rule) => ({
      field: rule.field,
      operator: rule.operator,
      value: rule.value,
    })),
    match_branch: "match",
    default_branch: "other",
  };
}

export function readConditions(value: Json | undefined): BusinessCondition[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((rule) => {
    if (!rule || typeof rule !== "object" || Array.isArray(rule)) return [];
    const field = businessFields.find((field) => field === rule.field);
    const operator = conditionOperators.find(
      (operator) => operator === rule.operator,
    );
    return field && operator && typeof rule.value === "string"
      ? [{ field, operator, value: rule.value }]
      : [];
  });
}
