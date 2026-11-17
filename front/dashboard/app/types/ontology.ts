/** Browser contracts contain source only for explicitly authorized authoring. */
export interface GolSourceFile {
  path: string;
  text: string;
}
export interface GolSummary {
  ontologyId: string;
  displayName: string;
  sourceRevision: string;
  activeGeneration: string | null;
  activeSourceRevision: string | null;
  status: "draft" | "published";
}
export interface GolDiagnostic {
  code: string;
  message: string;
  path: string;
  start: number;
  end: number;
  severity: string;
}
export interface GolDependency {
  ontologyId: string;
  generation: string;
  sourceRevision: string;
}
export interface GolWorkspace {
  summary: GolSummary;
  files: GolSourceFile[];
  diagnostics: GolDiagnostic[];
  dependencies: GolDependency[];
}
export interface GolValidation {
  ontologyId: string;
  sourceRevision: string;
  status: "valid" | "invalid" | "pending_authority";
  diagnostics: GolDiagnostic[];
  dependencies: GolDependency[];
}
export interface GolProperty {
  key: string;
  name: string;
  typeName: string;
}
export interface GolSymbol {
  key: string;
  name: string;
  kind: string;
  references: string[];
  properties: GolProperty[];
}
export interface GolSymbols {
  generation: string;
  symbols: GolSymbol[];
}
export interface GolPublication {
  ontologyId: string;
  sourceRevision: string;
  generation: string;
}
export type GolSchemaProfile = "STANDARD" | "OPEN_AI_STRICT" | "OUTLINES";
