import type { Json } from "~/utils/pipeline";
export interface OntologyResource {
  resource_id: string;
  kind: string;
  display_name: string;
  owner_id?: string;
  value_type?: string;
  references: string[];
  attributes: Record<string, Json>;
}
export interface SemanticDocument {
  version: string;
  resources: OntologyResource[];
  plugin_origin?: {
    package_id: string;
    plugin_version: string;
    package_version: string;
  };
  participant_mappings?: Record<string, string>;
  target_origin?: { ontology_id: string; revision_id: string };
}
export interface SemanticPackage {
  id: string;
  name: string;
  version: string;
  plugin_id: string;
  plugin_version: string;
  ontologies: Array<{ id: string; resources: OntologyResource[] }>;
}
export interface TenantOntology {
  ontologyId: string;
  displayName: string;
  productionPublication: { revisionId: string; publicationId: string } | null;
}
