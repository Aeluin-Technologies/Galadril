/** Validates semantic wire data before it enters editable tenant state. */
import type { Json } from "./pipeline";
import type {
  OntologyResource,
  SemanticDocument,
  SemanticPackage,
  TenantOntology,
  OntologyWorkspace,
  OntologyHistoryPage,
} from "../types/ontology";

export function officialOntologyPackageKey(packageId: string | undefined) {
  if (packageId === "law.socle") return "officialOntologies.law_socle";
  if (packageId === "law.ecritures") return "officialOntologies.law_ecritures";
  if (packageId === "law.moyens") return "officialOntologies.law_moyens";
  if (packageId === "law.preuves") return "officialOntologies.law_preuves";
  if (packageId === "law.contentieux")
    return "officialOntologies.law_contentieux";
  if (packageId === "law.administratif")
    return "officialOntologies.law_administratif";
  if (packageId === "law.judiciaire")
    return "officialOntologies.law_judiciaire";
  if (packageId === "law.penal") return "officialOntologies.law_penal";
  if (packageId === "utils.mail") return "officialOntologies.utils_mail";
}

export function officialOntologyResourceKey(
  resource: OntologyResource,
  packageId: string | undefined,
  preview = false,
) {
  const key = officialOntologyPackageKey(packageId);
  if (!key) return;
  const legal = packageId?.startsWith("law.") === true;
  const modulePrefix = legal ? "law." : "utils.";
  const module = resource.attributes.plugin_module;
  if (
    !preview &&
    !(typeof module === "string" && module.startsWith(modulePrefix))
  )
    return;
  const source = resource.attributes.plugin_package_id;
  const sourceKey =
    typeof source === "string" && source.startsWith(modulePrefix)
      ? officialOntologyPackageKey(source)
      : undefined;
  const catalog =
    sourceKey ??
    (legal &&
    !["law.socle", "law.ecritures", "law.moyens", "law.preuves"].includes(
      packageId ?? "",
    )
      ? "officialOntologies.law_contentieux"
      : key);
  return `${catalog}.resources.${resource.resource_id.replaceAll(".", "_")}`;
}

function canonicalDefinition(document: SemanticDocument): string {
  return JSON.stringify(
    {
      ...document,
      resources: [...document.resources].sort((a, b) =>
        a.resource_id.localeCompare(b.resource_id),
      ),
    },
    (_key, value: unknown) =>
      value !== null && typeof value === "object" && !Array.isArray(value)
        ? Object.fromEntries(
            Object.entries(object(value)).sort(([a], [b]) =>
              a.localeCompare(b),
            ),
          )
        : value,
  );
}

/** Tenant commits can change publication metadata without changing the model. */
export function sameOntologyDefinition(
  left: SemanticDocument | undefined,
  right: SemanticDocument | undefined,
): boolean {
  return (
    !!left &&
    !!right &&
    canonicalDefinition(left) === canonicalDefinition(right)
  );
}

export function ontologyPrimaryAction(state: {
  revision?: string;
  dirty: boolean;
  published: boolean;
}): "review" | "publish" | "edit" {
  if (!state.revision || state.dirty) return "review";
  return state.published ? "edit" : "publish";
}

export function withOntologyLink(
  document: SemanticDocument,
  id: string,
  source: string,
  target: string,
): SemanticDocument {
  if (
    document.plugin_origin ||
    document.resources.length >= 1024 ||
    !/^[a-z][a-z0-9_.-]{0,255}$/.test(id) ||
    document.resources.some((resource) => resource.resource_id === id)
  )
    throw new Error("ONTOLOGY_LINK_INVALID");
  const endpoints = new Set(
    document.resources
      .filter((resource) =>
        ["object_type", "event_type"].includes(resource.kind),
      )
      .map((resource) => resource.resource_id),
  );
  if (!endpoints.has(source) || !endpoints.has(target))
    throw new Error("ONTOLOGY_LINK_INVALID");
  return {
    ...document,
    resources: [
      ...document.resources,
      {
        resource_id: id,
        kind: "link_type",
        display_name: "Relationship",
        references: [source, target],
        attributes: { source_type: source, target_type: target },
      },
    ],
  };
}
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
          resources: array(module.resources).map((input) => {
            const value = resource(input);
            if (typeof module.package_id !== "string") return value;
            return {
              ...value,
              attributes: {
                ...value.attributes,
                plugin_package_id: text(module.package_id),
                plugin_package_name: text(
                  module.package_name ?? module.package_id,
                ),
              },
            };
          }),
        };
      }),
    };
  });
}
function revision(value: unknown): string {
  const result = text(value);
  if (!/^[A-Za-z0-9_-]{20,128}$/.test(result))
    throw new Error("ONTOLOGY_WIRE_INVALID");
  return result;
}
function timestamp(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0)
    throw new Error("ONTOLOGY_WIRE_INVALID");
  return value;
}
export function parseOntologyWorkspace(input: unknown): OntologyWorkspace {
  const value = object(decoded(input));
  const metadata = value.authoring ? object(value.authoring) : {};
  const entries = Object.entries(object(metadata.branches ?? {}));
  if (entries.length > 20) throw new Error("ONTOLOGY_WIRE_INVALID");
  return {
    revision_id: revision(value.revision_id),
    ontology: parseSemanticDocument(value.ontology),
    ...(value.display_name ? { display_name: text(value.display_name) } : {}),
    authoring: {
      deleted_at_ms:
        metadata.deleted_at_ms == null
          ? null
          : timestamp(metadata.deleted_at_ms),
      branches: Object.fromEntries(
        entries.map(([name, input]) => {
          if (name === "main" || !/^[A-Za-z0-9_-]{1,128}$/.test(name))
            throw new Error("ONTOLOGY_WIRE_INVALID");
          const branch = object(input);
          return [
            name,
            {
              parent_branch: text(branch.parent_branch),
              definition: parseSemanticDocument(branch.definition),
              display_name: text(branch.display_name),
              base_revision_id: revision(branch.base_revision_id),
              author: text(branch.author),
              updated_at_ms: timestamp(branch.updated_at_ms),
            },
          ];
        }),
      ),
    },
  };
}
export function parseOntologyHistory(input: unknown): OntologyHistoryPage {
  const value = object(decoded(input));
  const items = array(value.items);
  if (items.length > 100) throw new Error("ONTOLOGY_WIRE_INVALID");
  return {
    items: items.map((input) => {
      const item = object(input);
      return {
        revision_id: revision(item.revision_id),
        author: text(item.author),
        message: text(item.message),
        branch: text(item.branch),
        operation: text(item.operation),
        created_at_ms: timestamp(item.created_at_ms),
      };
    }),
    next_revision_id:
      value.next_revision_id == null ? null : revision(value.next_revision_id),
  };
}

/** Entries originate from Gateway's per-ontology authorized catalog, never a public plugin package. */
export function searchOntologies(
  query: string,
  items: readonly TenantOntology[],
  description: string,
) {
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length || query.length > 512) return [];
  return items
    .filter(
      (item) =>
        /^[A-Za-z0-9_.:-]{1,128}$/.test(item.ontologyId) &&
        words.every((word) =>
          `${item.displayName} ${item.ontologyId}`
            .toLocaleLowerCase()
            .includes(word),
        ),
    )
    .slice(0, 8)
    .map((item) => ({
      id: `ontology-${Array.from(item.ontologyId, (character) => character.charCodeAt(0).toString(16)).join("")}`,
      kind: "navigation" as const,
      href: `/ontology/edit/${encodeURIComponent(item.ontologyId)}`,
      label: item.displayName,
      description,
      score: 1000,
    }));
}
