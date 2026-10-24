/** Keeps the authored graph distinct from Registry's immutable execution graph. */
export type Json =
  string | number | boolean | null | Json[] | { [key: string]: Json };
export type StepKind =
  | "source"
  | "inference"
  | "resolve"
  | "sink"
  | "causal"
  | "dbt"
  | "algorithm"
  | "route"
  | "pipeline";
export type RoutingKind = "regex" | "slm" | "metadata";
export interface PipelineReference {
  pipeline_id: string;
  revision_id: string;
  input_source: string;
}
export interface NodeConfig {
  topic?: string;
  match_pattern?: string;
  schema_path?: string;
  parser?: string;
  source_kind?: string;
  group_id?: string;
  content_types?: string[];
  model?: string;
  artifact_path?: string;
  capability_id?: string;
  capability_version?: string;
  routing?: RoutingKind;
  branch_labels?: string[];
  pipeline_ref?: PipelineReference;
  params?: Record<string, Json>;
}
export interface GraphNode {
  id: string;
  data: { stepType: string; config: NodeConfig };
}
export interface GraphEdge {
  source: string;
  target: string;
  sourceHandle?: string | null;
}
export interface SourceDefinition {
  capability_id?: string;
  capability_version?: string;
  id: string;
  topic: string;
  match_pattern: string;
  schema_path: string;
  parser?: string;
  source_kind?: string;
  group_id?: string;
  content_types?: string[];
}
export interface StepDefinition {
  step: string;
  type: string;
  input_from: string[];
  model?: string;
  capability_id?: string;
  capability_version?: string;
  artifact_path?: string;
  routing?: RoutingKind;
  pipeline_ref?: PipelineReference;
  params?: Record<string, Json>;
  branches?: Record<string, string[]>;
}
export interface Definition {
  name: string;
  version?: number;
  sources: SourceDefinition[];
  pipeline: StepDefinition[];
}
export interface PipelineArtifact {
  pipeline_id: string;
  name: string;
  head_revision_id: string;
  published_revision_id: string | null;
  definition: Definition;
}
export type PipelineSummary = Omit<PipelineArtifact, "definition">;

function copyJson(value: Json): Json {
  if (Array.isArray(value)) return value.map(copyJson);
  if (value !== null && typeof value === "object")
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [key, copyJson(item)]),
    );
  return value;
}

/** Reads reactive values into plain data before history or draft ownership changes. */
export function copyConfig(config: NodeConfig): NodeConfig {
  return {
    ...config,
    params:
      config.params &&
      Object.fromEntries(
        Object.entries(config.params).map(([key, value]) => [
          key,
          copyJson(value),
        ]),
      ),
    pipeline_ref: config.pipeline_ref && { ...config.pipeline_ref },
    branch_labels: config.branch_labels && [...config.branch_labels],
    content_types: config.content_types && [...config.content_types],
  };
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function json(value: unknown, depth = 0): value is Json {
  if (depth > 32) return false;
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return true;
  if (typeof value === "number") return Number.isFinite(value);
  return Array.isArray(value)
    ? value.every((item) => json(item, depth + 1))
    : record(value) &&
        Object.values(value).every((item) => json(item, depth + 1));
}

/** GraphQL's native JSON scalar uses an explicit JSON string representation. */
export function parseDefinition(value: unknown): Definition {
  const decoded: unknown =
    typeof value === "string" ? JSON.parse(value) : value;
  if (!isDefinition(decoded)) throw new Error("PIPELINE_UNAVAILABLE");
  return decoded;
}

function isDefinition(value: unknown): value is Definition {
  if (
    !record(value) ||
    typeof value.name !== "string" ||
    !Array.isArray(value.sources) ||
    !Array.isArray(value.pipeline)
  )
    return false;
  return (
    value.sources.every(
      (source) =>
        record(source) &&
        ["id", "topic", "match_pattern", "schema_path"].every(
          (key) => typeof source[key] === "string",
        ),
    ) &&
    value.pipeline.every((step) => {
      if (
        !record(step) ||
        typeof step.step !== "string" ||
        typeof step.type !== "string" ||
        !Array.isArray(step.input_from) ||
        !step.input_from.every((input) => typeof input === "string")
      )
        return false;
      if (
        step.params !== undefined &&
        (!record(step.params) || !json(step.params))
      )
        return false;
      const reference = step.pipeline_ref;
      if (
        reference !== undefined &&
        reference !== null &&
        (!record(reference) ||
          !["pipeline_id", "revision_id", "input_source"].every(
            (key) => typeof reference[key] === "string",
          ))
      )
        return false;
      return [
        "model",
        "capability_id",
        "capability_version",
        "artifact_path",
      ].every(
        (key) =>
          step[key] === undefined ||
          step[key] === null ||
          typeof step[key] === "string",
      );
    })
  );
}

export function branchLabels(config: NodeConfig): string[] {
  return (
    config.branch_labels ?? [
      String(config.params?.match_branch ?? "match"),
      String(config.params?.default_branch ?? "other"),
    ]
  );
}

/** Rejects invalid links immediately; full expanded limits remain server-owned. */
export function validateConnections(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): void {
  if (nodes.length > 256) throw new Error("blocks");
  if (nodes.filter((node) => node.data.stepType === "pipeline").length > 3)
    throw new Error("transfers");
  const byId = new Map(nodes.map((node) => [node.id, node]));
  if (byId.size !== nodes.length) throw new Error("duplicate");
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const children = new Map<string, string[]>();
  const incoming = new Map<string, Set<string>>();
  const seen = new Set<string>();
  for (const edge of edges) {
    const source = byId.get(edge.source),
      target = byId.get(edge.target);
    if (
      !source ||
      !target ||
      source.data.stepType === "pipeline" ||
      target.data.stepType === "source"
    )
      throw new Error("connection");
    const key = JSON.stringify([
      edge.source,
      edge.target,
      edge.sourceHandle ?? "",
    ]);
    if (seen.has(key)) throw new Error("duplicate");
    seen.add(key);
    if (
      source.data.stepType === "route" &&
      !branchLabels(source.data.config).includes(edge.sourceHandle ?? "")
    )
      throw new Error("branch");
    const parents = incoming.get(edge.target) ?? new Set<string>();
    parents.add(edge.source);
    incoming.set(edge.target, parents);
    if (parents.size > 1) throw new Error("join");
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
    const next = children.get(edge.source) ?? [];
    next.push(edge.target);
    children.set(edge.source, next);
  }
  const ready = [...indegree]
    .filter(([, count]) => count === 0)
    .map(([id]) => id);
  let visited = 0;
  for (let index = 0; index < ready.length; index++) {
    const id = ready[index];
    if (!id) continue;
    visited++;
    for (const child of children.get(id) ?? []) {
      const count = (indegree.get(child) ?? 0) - 1;
      indegree.set(child, count);
      if (count === 0) ready.push(child);
    }
  }
  if (visited !== nodes.length) throw new Error("cycle");
}

export function definitionFromGraph(
  name: string,
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): Definition {
  validateConnections(nodes, edges);
  if (!name.trim()) throw new Error("name");
  const sources: SourceDefinition[] = [];
  const pipeline: StepDefinition[] = [];
  for (const node of nodes) {
    const config = node.data.config;
    if (node.data.stepType === "source") {
      sources.push({
        id: node.id,
        capability_id: config.capability_id,
        capability_version: config.capability_version,
        topic: config.topic ?? "raw",
        match_pattern: config.match_pattern ?? ".*",
        schema_path: config.schema_path ?? "schemas/avro/text.avsc",
        parser: config.parser,
        source_kind: config.source_kind,
        group_id: config.group_id,
        content_types: config.content_types,
      });
      continue;
    }
    const inputs = [
      ...new Set(
        edges
          .filter((edge) => edge.target === node.id)
          .map((edge) => edge.source),
      ),
    ];
    if (
      inputs.length !== 1 &&
      config.params?.trigger !== "cron" &&
      config.params?.trigger !== "on_demand"
    )
      throw new Error("input");
    const step: StepDefinition = {
      step: node.id,
      type: node.data.stepType,
      input_from: inputs,
      model: config.model,
      capability_id: config.capability_id,
      capability_version: config.capability_version,
      artifact_path: config.artifact_path,
      routing: config.routing,
      pipeline_ref: config.pipeline_ref,
      params: config.params ?? {},
    };
    if (step.type === "route") {
      step.branches = Object.fromEntries(
        branchLabels(config).map((label) => {
          const targets = edges
            .filter(
              (edge) => edge.source === node.id && edge.sourceHandle === label,
            )
            .map((edge) => edge.target);
          if (!label.trim() || !targets.length) throw new Error("branch");
          return [label, targets];
        }),
      );
    }
    if (step.type === "pipeline" && !step.pipeline_ref)
      throw new Error("reference");
    pipeline.push(step);
  }
  if (!sources.length) throw new Error("source");
  return { name: name.trim(), version: 1, sources, pipeline };
}
