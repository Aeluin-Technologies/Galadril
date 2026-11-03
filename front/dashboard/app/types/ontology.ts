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
export interface OntologyBranch {
  parent_branch: string;
  definition: SemanticDocument;
  display_name: string;
  base_revision_id: string;
  author: string;
  updated_at_ms: number;
}
export interface OntologyWorkspace {
  revision_id: string;
  ontology: SemanticDocument;
  display_name?: string;
  authoring: {
    branches: Record<string, OntologyBranch>;
    deleted_at_ms: number | null;
  };
}
export type OntologyCommand =
  | { operation: "create_branch"; branch: string; from_branch: string }
  | { operation: "merge" | "delete_branch"; branch: string }
  | { operation: "delete" | "restore" };
export interface OntologyHistoryItem {
  revision_id: string;
  author: string;
  message: string;
  branch: string;
  operation: string;
  created_at_ms: number;
}
export interface OntologyHistoryPage {
  items: OntologyHistoryItem[];
  next_revision_id: string | null;
}
