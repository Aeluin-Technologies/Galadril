import { parseDefinition } from "./pipeline";
import type { Definition, Json } from "./pipeline";

export interface ReviewConflict {
  path: string;
  base: Json | undefined;
  target: Json | undefined;
  source: Json | undefined;
}
export interface MergePreview {
  definition: Definition;
  conflicts: ReviewConflict[];
}
export interface DefinitionChange {
  path: string;
  before: Json | undefined;
  after: Json | undefined;
}
function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function object(value: unknown): Record<string, unknown> {
  if (isRecord(value)) return value;
  throw new Error("PIPELINE_REVIEW_INVALID");
}
function json(value: unknown, depth = 0): Json | undefined {
  if (value === undefined) return undefined;
  if (depth > 32) throw new Error("PIPELINE_REVIEW_INVALID");
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value))
    return value.map((item) => json(item, depth + 1) ?? null);
  return Object.fromEntries(
    Object.entries(object(value)).map(([key, item]) => [
      key,
      json(item, depth + 1) ?? null,
    ]),
  );
}
function path(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096)
    throw new Error("PIPELINE_REVIEW_INVALID");
  return value;
}
function decoded(value: unknown): unknown {
  return typeof value === "string" ? JSON.parse(value) : value;
}
export function parsePreview(input: unknown): MergePreview {
  const value = object(decoded(input));
  if (!Array.isArray(value.conflicts))
    throw new Error("PIPELINE_REVIEW_INVALID");
  return {
    definition: parseDefinition(value.definition),
    conflicts: value.conflicts.map((input) => {
      const conflict = object(input);
      return {
        path: path(conflict.path),
        base: json(conflict.base),
        target: json(conflict.target),
        source: json(conflict.source),
      };
    }),
  };
}
export function parseChanges(input: unknown): DefinitionChange[] {
  const value = decoded(input);
  if (!Array.isArray(value)) throw new Error("PIPELINE_REVIEW_INVALID");
  return value.map((input) => {
    const item = object(input);
    return {
      path: path(item.path),
      before: json(item.before),
      after: json(item.after),
    };
  });
}

export function parseDiagnostics(
  input: unknown,
): import("./pipeline").PipelineDiagnostic[] {
  const value = decoded(input);
  if (!Array.isArray(value)) throw new Error("PIPELINE_REVIEW_INVALID");
  const codes = [
    "source",
    "input",
    "model",
    "field",
    "branch",
    "cycle",
    "connection",
    "duplicate",
    "join",
    "blocks",
    "transfers",
    "reference",
    "backend",
  ] as const;
  return value.map((input) => {
    const issue = object(input);
    const code = codes.find((code) => code === issue.code);
    if (!code) throw new Error("PIPELINE_REVIEW_INVALID");
    return {
      code,
      nodeId: typeof issue.node_id === "string" ? issue.node_id : undefined,
      field: typeof issue.field === "string" ? issue.field : undefined,
    };
  });
}
