import type { Json, NodeConfig, StepKind } from "./pipeline";

export interface PluginBlock {
  id: string;
  pluginId: string;
  pluginVersion: string;
  origin: string;
  execution: string;
  kind: string;
  targets: string[];
  name: string;
  description: string;
  stepType?: string | null;
  parametersSchemaJson: string;
  compatibility: { allowedModels: string[]; requires: string[] };
}
const kinds: readonly string[] = [
  "source",
  "inference",
  "algorithm",
  "resolve",
  "sink",
  "causal",
  "dbt",
];
export function blockKind(value: string | null | undefined): StepKind {
  switch (value) {
    case "source":
    case "inference":
    case "algorithm":
    case "resolve":
    case "sink":
    case "causal":
    case "dbt":
      return value;
    default:
      throw new Error("PLUGIN_UNAVAILABLE");
  }
}
export function pluginBlocks(catalog: readonly PluginBlock[]): PluginBlock[] {
  return catalog.filter(
    (item) =>
      item.origin === "PLUGIN" &&
      item.execution === "REGISTERED" &&
      item.targets.includes("PIPELINE") &&
      kinds.includes(item.stepType ?? ""),
  );
}
function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function isJson(value: unknown, depth = 0): value is Json {
  if (depth > 32) return false;
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return true;
  if (typeof value === "number") return Number.isFinite(value);
  return Array.isArray(value)
    ? value.every((item) => isJson(item, depth + 1))
    : object(value) &&
        Object.values(value).every((item) => isJson(item, depth + 1));
}
export function parseParameters(text: string): Record<string, Json> {
  if (text.length > 65536) throw new Error("PLUGIN_PARAMETERS");
  const value: unknown = JSON.parse(text);
  if (!object(value) || !isJson(value)) throw new Error("PLUGIN_PARAMETERS");
  return value;
}
export interface ParameterField {
  key: string;
  label: string;
  type: string;
  required: boolean;
  options?: string[];
}
export function parameterFields(capability: PluginBlock): ParameterField[] {
  const schema = parseParameters(capability.parametersSchemaJson);
  if (!object(schema.properties)) return [];
  const required = Array.isArray(schema.required) ? schema.required : [];
  return Object.entries(schema.properties).flatMap(([key, property]) => {
    if (
      !object(property) ||
      typeof property.type !== "string" ||
      !["string", "integer", "number", "boolean"].includes(property.type)
    )
      return [];
    return [
      {
        key,
        label: typeof property.title === "string" ? property.title : key,
        type: property.type,
        required: required.includes(key),
        options:
          Array.isArray(property.enum) &&
          property.enum.every((item) => typeof item === "string")
            ? property.enum
            : undefined,
      },
    ];
  });
}
/** Defaults come from trusted metadata; executable bindings never enter authored data. */
export function pluginConfig(capability: PluginBlock): NodeConfig {
  if (!pluginBlocks([capability]).length) throw new Error("PLUGIN_UNAVAILABLE");
  const schema = parseParameters(capability.parametersSchemaJson);
  const defaults = object(schema.properties)
    ? Object.fromEntries(
        Object.entries(schema.properties).flatMap(([key, value]) =>
          object(value) && value.default !== undefined && isJson(value.default)
            ? [[key, value.default]]
            : [],
        ),
      )
    : {};
  const config: NodeConfig = {
    capability_id: capability.id,
    capability_version: capability.pluginVersion,
    ...(capability.compatibility.allowedModels[0]
      ? { model: capability.compatibility.allowedModels[0] }
      : {}),
  };
  return capability.stepType === "source"
    ? sourceConfig(config, defaults)
    : { ...config, params: defaults };
}
export function sourceParameters(config: NodeConfig): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(config).filter(
      ([key, value]) =>
        !["capability_id", "capability_version", "params"].includes(key) &&
        value !== undefined &&
        isJson(value),
    ),
  );
}
export function sourceConfig(
  config: NodeConfig,
  parameters: Record<string, Json>,
): NodeConfig {
  const read = (key: string): string | undefined =>
    typeof parameters[key] === "string" ? parameters[key] : undefined;
  const types = parameters.content_types;
  return {
    capability_id: config.capability_id,
    capability_version: config.capability_version,
    topic: read("topic") ?? "raw",
    match_pattern: read("match_pattern") ?? ".*",
    schema_path: read("schema_path") ?? "schemas/avro/text.avsc",
    parser: read("parser"),
    source_kind: read("source_kind"),
    group_id: read("group_id"),
    content_types:
      Array.isArray(types) && types.every((item) => typeof item === "string")
        ? types
        : undefined,
  };
}
