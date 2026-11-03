import type { SemanticDocument } from "../types/ontology";
import { withOntologyLink } from "./ontology.ts";

export interface OntologyTypeDraft {
  id: string;
  name: string;
  definition: string;
  kind: "object_type" | "event_type";
  example: string;
}

export interface OntologyLinkDraft {
  id: string;
  name: string;
  source: string;
  target: string;
  existing: boolean;
}

export function withOntologyType(
  document: SemanticDocument,
  input: OntologyTypeDraft,
): SemanticDocument {
  if (
    document.plugin_origin ||
    document.resources.length >= 1024 ||
    !/^[a-z][a-z0-9_.-]{0,255}$/.test(input.id) ||
    !input.name.trim() ||
    input.name.length > 256 ||
    !input.definition.trim() ||
    input.definition.length > 4096 ||
    input.example.length > 4096 ||
    !["object_type", "event_type"].includes(input.kind) ||
    document.resources.some((resource) => resource.resource_id === input.id)
  )
    throw new Error("ONTOLOGY_TYPE_INVALID");
  return {
    ...document,
    resources: [
      ...document.resources,
      {
        resource_id: input.id,
        kind: input.kind,
        display_name: input.name.trim(),
        references: [],
        attributes: {
          description: input.definition.trim(),
          ...(input.example.trim() ? { example: input.example.trim() } : {}),
        },
      },
    ],
  };
}

export function withUpdatedOntologyLink(
  document: SemanticDocument,
  id: string,
  source: string,
  target: string,
  name: string,
): SemanticDocument {
  const previous = document.resources.find(
    (resource) => resource.resource_id === id,
  );
  if (previous?.kind !== "link_type" || !name.trim() || name.length > 256)
    throw new Error("ONTOLOGY_LINK_INVALID");
  const candidate = withOntologyLink(
    {
      ...document,
      resources: document.resources.filter(
        (resource) => resource.resource_id !== id,
      ),
    },
    id,
    source,
    target,
  );
  const replacement = candidate.resources.at(-1);
  if (!replacement) throw new Error("ONTOLOGY_LINK_INVALID");
  const references = [
    ...new Set([
      ...previous.references.filter(
        (reference) =>
          reference !== previous.attributes.source_type &&
          reference !== previous.attributes.target_type,
      ),
      source,
      target,
    ]),
  ];
  return {
    ...document,
    resources: document.resources.map((resource) =>
      resource.resource_id === id
        ? {
            ...replacement,
            display_name: name.trim(),
            references,
            attributes: {
              ...previous.attributes,
              source_type: source,
              target_type: target,
            },
          }
        : resource,
    ),
  };
}

export function withoutOntologyLink(
  document: SemanticDocument,
  id: string,
): SemanticDocument {
  if (
    document.plugin_origin ||
    !document.resources.some(
      (resource) =>
        resource.resource_id === id && resource.kind === "link_type",
    ) ||
    document.resources.some(
      (resource) =>
        resource.resource_id !== id && resource.references.includes(id),
    )
  )
    throw new Error("ONTOLOGY_LINK_IN_USE");
  return {
    ...document,
    resources: document.resources.filter(
      (resource) => resource.resource_id !== id,
    ),
  };
}
