/** Validates semantic wire data before it enters editable tenant state. */
import type { Json } from "./pipeline";
import type {
  OntologyResource,
  SemanticDocument,
  SemanticPackage,
} from "../types/ontology";
function object(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("ONTOLOGY_WIRE_INVALID");
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  if (typeof value !== "string" || value.length > 4096)
    throw new Error("ONTOLOGY_WIRE_INVALID");
  return value;
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value) || value.length > 1024)
    throw new Error("ONTOLOGY_WIRE_INVALID");
  return value;
}
function decoded(value: unknown): unknown {
  return typeof value === "string" ? JSON.parse(value) : value;
}
function json(value: unknown, depth = 0): Json {
  if (depth > 16) throw new Error("ONTOLOGY_WIRE_INVALID");
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return value;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (Array.isArray(value))
    return array(value).map((item) => json(item, depth + 1));
  return Object.fromEntries(
    Object.entries(object(value)).map(([key, item]) => [
      key,
      json(item, depth + 1),
    ]),
  );
}
function attributes(value: unknown): Record<string, Json> {
  return Object.fromEntries(
    Object.entries(object(value)).map(([key, item]) => [key, json(item)]),
  );
}
function resource(input: unknown): OntologyResource {
  const value = object(input),
    kind = text(value.kind);
  if (
    ![
      "object_type",
      "property",
      "link_type",
      "event_type",
      "action",
      "function",
    ].includes(kind)
  )
    throw new Error("ONTOLOGY_WIRE_INVALID");
  return {
    resource_id: text(value.resource_id),
    kind,
    display_name: text(value.display_name),
    references: array(value.references ?? []).map(text),
    attributes: attributes(value.attributes),
    ...(value.owner_id ? { owner_id: text(value.owner_id) } : {}),
    ...(value.value_type ? { value_type: text(value.value_type) } : {}),
  };
}
export function parseSemanticDocument(input: unknown): SemanticDocument {
  const value = object(decoded(input));
  const result: SemanticDocument = {
    version: text(value.version),
    resources: array(value.resources).map(resource),
  };
  if (value.plugin_origin) {
    const origin = object(value.plugin_origin);
    result.plugin_origin = {
      package_id: text(origin.package_id),
      plugin_version: text(origin.plugin_version ?? ""),
      package_version: text(origin.package_version),
    };
  }
  if (value.target_origin) {
    const target = object(value.target_origin);
    result.target_origin = {
      ontology_id: text(target.ontology_id),
      revision_id: text(target.revision_id),
    };
  }
  if (value.participant_mappings)
    result.participant_mappings = Object.fromEntries(
      Object.entries(object(value.participant_mappings)).map(([key, item]) => [
        key,
        text(item),
      ]),
    );
  return result;
}
export function parseSemanticPackages(input: unknown): SemanticPackage[] {
  return array(object(decoded(input)).packages).map((input) => {
    const value = object(input);
    return {
      id: text(value.id),
      name: text(value.name),
      version: text(value.version),
      plugin_id: text(value.plugin_id),
      plugin_version: text(value.plugin_version),
      ontologies: array(value.ontologies).map((input) => {
        const module = object(input);
        return {
          id: text(module.id),
          resources: array(module.resources).map(resource),
        };
      }),
    };
  });
}
export function parseOntologyWorkspace(input: unknown): {
  revision_id: string;
  ontology: SemanticDocument;
} {
  const value = object(decoded(input));
  return {
    revision_id: text(value.revision_id),
    ontology: parseSemanticDocument(value.ontology),
  };
}
