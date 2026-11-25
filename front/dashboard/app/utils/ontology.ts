/** Validate the GOL transport boundary without duplicating compiler semantics. */
import type {
  GolDependency,
  GolDiagnostic,
  GolPublication,
  GolSourceFile,
  GolSummary,
  GolSymbols,
  GolValidation,
  GolWorkspace,
} from "../types/ontology";

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function object(value: unknown): Record<string, unknown> {
  if (!isRecord(value)) throw new Error("GOL_WIRE_INVALID");
  return value;
}
function decode(value: unknown): unknown {
  return typeof value === "string" ? JSON.parse(value) : value;
}
function text(value: unknown, maximum = 4096): string {
  if (typeof value !== "string" || value.length > maximum)
    throw new Error("GOL_WIRE_INVALID");
  return value;
}
function optionalText(value: unknown): string | null {
  return value == null ? null : text(value);
}
function list(value: unknown): unknown[] {
  if (!Array.isArray(value) || value.length > 10000)
    throw new Error("GOL_WIRE_INVALID");
  return value;
}
function offset(value: unknown): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 0)
    throw new Error("GOL_WIRE_INVALID");
  return value;
}
function sourceFile(value: unknown): GolSourceFile {
  const item = object(value),
    path = text(item.path, 512);
  if (
    !path.endsWith(".gol") ||
    path.startsWith("/") ||
    path.includes("\\") ||
    path.split("/").some((part) => !part || part === "." || part === "..")
  )
    throw new Error("GOL_WIRE_INVALID");
  return { path, text: text(item.text, 4 * 1024 * 1024) };
}
function diagnostics(value: unknown): GolDiagnostic[] {
  return list(value).map((raw) => {
    const item = object(raw),
      start = offset(item.start),
      end = offset(item.end);
    if (end < start) throw new Error("GOL_WIRE_INVALID");
    return {
      code: text(item.code),
      message: text(item.message),
      path: text(item.path),
      start,
      end,
      severity: text(item.severity),
    };
  });
}
function dependencies(value: unknown): GolDependency[] {
  return list(value).map((raw) => {
    const item = object(raw);
    return {
      ontologyId: text(item.ontologyId),
      generation: text(item.generation),
      sourceRevision: text(item.sourceRevision),
    };
  });
}
export function parseGolSummary(input: unknown): GolSummary {
  const value = object(input),
    status = value.status;
  if (status !== "draft" && status !== "published")
    throw new Error("GOL_WIRE_INVALID");
  return {
    ontologyId: text(value.ontologyId),
    displayName: text(value.displayName),
    sourceRevision: text(value.sourceRevision),
    activeGeneration: optionalText(value.activeGeneration),
    activeSourceRevision: optionalText(value.activeSourceRevision),
    status,
  };
}
export function parseGolSummaries(input: unknown): GolSummary[] {
  return list(decode(input)).map(parseGolSummary);
}
export function parseGolWorkspace(input: unknown): GolWorkspace {
  const value = object(decode(input));
  return {
    summary: parseGolSummary(value.summary),
    files: list(value.files).map(sourceFile),
    diagnostics: diagnostics(value.diagnostics),
    dependencies: dependencies(value.dependencies),
  };
}
export function parseGolValidation(input: unknown): GolValidation {
  const value = object(decode(input)),
    status = value.status;
  if (
    status !== "valid" &&
    status !== "invalid" &&
    status !== "pending_authority"
  )
    throw new Error("GOL_WIRE_INVALID");
  return {
    ontologyId: text(value.ontologyId),
    sourceRevision: text(value.sourceRevision),
    status,
    diagnostics: diagnostics(value.diagnostics),
    dependencies: dependencies(value.dependencies),
  };
}
export function parseGolPublication(input: unknown): GolPublication {
  const value = object(decode(input));
  return {
    ontologyId: text(value.ontologyId),
    sourceRevision: text(value.sourceRevision),
    generation: text(value.generation),
  };
}
export function parseGolSymbols(input: unknown): GolSymbols {
  const value = object(decode(input)),
    generation = text(value.generation);
  if (!generation) throw new Error("GOL_WIRE_INVALID");
  return {
    generation,
    symbols: list(value.symbols).map((raw) => {
      const item = object(raw);
      return {
        key: text(item.key),
        name: text(item.name),
        kind: text(item.kind),
        references: list(item.references).map((value) => text(value)),
        properties: list(item.properties).map((raw) => {
          const property = object(raw);
          return {
            key: text(property.key),
            name: text(property.name),
            typeName: text(property.typeName),
          };
        }),
      };
    }),
  };
}
/** Clamp byte spans to complete Unicode characters before selecting editor text. */
export function diagnosticPosition(
  source: string,
  first: number,
  last: number,
) {
  const boundary = (requested: number) => {
    let bytes = 0,
      units = 0;
    for (const character of source) {
      const code = character.charCodeAt(0);
      const size =
        character.length === 2 ? 4 : code <= 0x7f ? 1 : code <= 0x7ff ? 2 : 3;
      if (bytes + size > requested) break;
      bytes += size;
      units += character.length;
    }
    return units;
  };
  const start = boundary(Math.max(0, first));
  const end = Math.max(start, boundary(Math.max(first, last)));
  const before = source.slice(0, start),
    newline = before.lastIndexOf("\n");
  return {
    start,
    end,
    line: before.split("\n").length,
    column: Array.from(before.slice(newline + 1)).length + 1,
  };
}
export function sourceChanged(
  left: readonly GolSourceFile[],
  right: readonly GolSourceFile[],
): boolean {
  return (
    left.length !== right.length ||
    left.some(
      (file, index) =>
        file.path !== right[index]?.path || file.text !== right[index]?.text,
    )
  );
}
export function searchOntologies(
  query: string,
  items: readonly GolSummary[],
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
      id: `ontology-${encodeURIComponent(item.ontologyId)}`,
      kind: "navigation" as const,
      href: `/ontology/edit/${encodeURIComponent(item.ontologyId)}`,
      label: item.displayName,
      description,
      score: 1000,
    }));
}

const draftPermissions = [
  { name: "read_access", relation: "read" },
  { name: "write_access", relation: "write" },
  { name: "describe", relation: "describe" },
  { name: "transfer", relation: "transfer" },
  { name: "execute", relation: "execute" },
]
  .map(
    ({ name, relation }) =>
      `    permission ${name} { relation: "${relation}"; cedar: ${JSON.stringify(`Gol::Action::"${relation}"`)}; }`,
  )
  .join("\n");

/** Explicit starter contracts require Registry approval and grant no permissions. */
export function golDraftSource(ontologyId: string): string {
  return `ontology ${ontologyId} version "1.0.0" {
  entity Example {
    field title: String;
  }
  access Example {
    resource: "gol_resource";
${draftPermissions}
  }
}
`;
}
