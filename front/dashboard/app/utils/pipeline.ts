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
  | "pipeline"
  | "forward"
  | "filter";
export type RoutingKind = "regex" | "slm" | "metadata" | "conditions";
export interface PipelineReference {
  pipeline_id: string;
  revision_id: string;
  input_source: string;
}
export interface NodeConfig {
  account_id?: string;
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
  position?: { x: number; y: number };
  id: string;
  data: { stepType: string; config: NodeConfig; title?: string };
}
export interface GraphEdge {
  source: string;
  target: string;
  sourceHandle?: string | null;
  targetHandle?: string | null;
}
export interface SourceDefinition {
  account_id?: string;
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
  editor?: {
    positions: Record<string, { x: number; y: number }>;
    labels?: Record<string, string>;
  };
}
export interface PipelineArtifact {
  pipeline_id: string;
  name: string;
  head_revision_id: string;
  published_revision_id: string | null;
  definition: Definition;
  lifecycle?: "draft" | "approved" | "published" | "deployed";
  authoring?: AuthoringState;
  branch_names?: string[];
}
export interface AuthoringState {
  branches: Record<
    string,
    {
      definition: Definition;
      base_definition: Definition;
      base_revision_id: string;
      author_id: string;
      updated_at_ms: number;
    }
  >;
  proposals: Record<
    string,
    {
      source_branch: string;
      source_hash: string;
      target_hash: string;
      author_id: string;
      created_at_ms: number;
    }
  >;
  approval: {
    definition_hash: string;
    author_id: string;
    reviewed_at_ms: number;
  } | null;
  release: {
    definition_hash: string;
    revision_id: string;
    author_id: string;
    published_at_ms: number;
  } | null;
}
export type AuthoringCommand =
  | { operation: "save_draft"; branch: string }
  | { operation: "create_branch"; branch: string; from_branch: string }
  | { operation: "propose_merge"; source_branch: string }
  | {
      operation: "merge";
      proposal_id: string;
      resolutions: Record<string, "target" | "source">;
    }
  | { operation: "approve" | "publish" | "deploy" };
export type PipelineSummary = Omit<PipelineArtifact, "definition">;

export interface CapabilityLabel {
  id: string;
  pluginVersion: string;
  name: string;
}
export interface StoredGraph {
  nodes: Array<
    GraphNode & {
      type: "custom";
      position: { x: number; y: number };
      data: { stepType: StepKind; config: NodeConfig; title?: string };
    }
  >;
  edges: Array<GraphEdge & { id: string }>;
}

/** Reconstruct authored fan-out and labeled routes without expanding linked pipelines. */
export function graphFromDefinition(
  value: unknown,
  capabilities: readonly CapabilityLabel[] = [],
): StoredGraph {
  const labels = new Map<string, string>();
  for (const item of capabilities) {
    labels.set(`${item.id}@${item.pluginVersion}`, item.name);
    labels.set(`${item.id}@`, item.name);
  }
  const title = (config: NodeConfig) =>
    config.capability_id
      ? (labels.get(
          `${config.capability_id}@${config.capability_version ?? ""}`,
        ) ?? config.capability_id)
      : undefined;
  const definition = parseDefinition(value);
  const nodes: StoredGraph["nodes"] = definition.sources.map(
    ({ id, ...config }, index) => ({
      id,
      type: "custom",
      position: { x: 40, y: 80 + index * 180 },
      data: {
        stepType: "source",
        config: copyConfig(config),
        title: definition.editor?.labels?.[id] ?? title(config),
      },
    }),
  );
  const edges: StoredGraph["edges"] = [];
  for (const [index, step] of definition.pipeline.entries()) {
    const kind = step.type;
    if (
      kind !== "inference" &&
      kind !== "resolve" &&
      kind !== "sink" &&
      kind !== "causal" &&
      kind !== "dbt" &&
      kind !== "algorithm" &&
      kind !== "route" &&
      kind !== "pipeline" &&
      kind !== "forward" &&
      kind !== "filter"
    )
      throw new Error("unsupported");
    const { step: id, type: _type, input_from, branches, ...config } = step;
    nodes.push({
      id,
      type: "custom",
      position: {
        x: 360 + Math.floor(index / 3) * 320,
        y: 80 + (index % 3) * 180,
      },
      data: {
        stepType: kind,
        title: definition.editor?.labels?.[id] ?? title(config),
        config: copyConfig({
          ...config,
          ...(branches ? { branch_labels: Object.keys(branches) } : {}),
        }),
      },
    });
    for (const source of input_from) {
      const parent = definition.pipeline.find(
        (candidate) => candidate.step === source,
      );
      const labels: Array<string | undefined> =
        parent?.type === "route"
          ? Object.entries(parent.branches ?? {})
              .filter(([, targets]) => targets.includes(id))
              .map(([label]) => label)
          : [undefined];
      if (!labels.length) labels.push(undefined);
      for (const label of labels)
        edges.push({
          id: `stored-${edges.length}`,
          source,
          target: id,
          sourceHandle: label,
        });
    }
  }
  if (new Set(nodes.map((node) => node.id)).size !== nodes.length)
    throw new Error("duplicate");
  for (const node of nodes) {
    const saved = definition.editor?.positions[node.id];
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y))
      node.position = { x: saved.x, y: saved.y };
  }
  return { nodes, edges };
}

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
    if (
      (edge.targetHandle && edge.targetHandle !== "record") ||
      (source.data.stepType !== "route" &&
        edge.sourceHandle &&
        edge.sourceHandle !== "record")
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
    if (
      parents.size > 1 &&
      !(
        target.data.stepType === "forward" &&
        target.data.config.params?.merge === "union"
      )
    )
      throw new Error("join");
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
  draft = false,
): Definition {
  if (!draft) validateConnections(nodes, edges);
  if (!draft && !name.trim()) throw new Error("name");
  const sources: SourceDefinition[] = [];
  const pipeline: StepDefinition[] = [];
  for (const node of nodes) {
    const config = node.data.config;
    if (node.data.stepType === "source") {
      sources.push({
        account_id: config.account_id,
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
      !draft &&
      inputs.length !== 1 &&
      !(
        node.data.stepType === "forward" &&
        config.params?.merge === "union" &&
        inputs.length > 0
      ) &&
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
          if (!draft && (!label.trim() || !targets.length))
            throw new Error("branch");
          return [label, targets];
        }),
      );
    }
    if (!draft && step.type === "pipeline" && !step.pipeline_ref)
      throw new Error("reference");
    pipeline.push(step);
  }
  if (!draft && !sources.length) throw new Error("source");
  return {
    name: name.trim(),
    version: 1,
    sources,
    pipeline,
    editor: {
      labels: Object.fromEntries(
        nodes
          .filter((node) => node.data.title)
          .map((node) => [node.id, node.data.title ?? ""]),
      ),
      positions: Object.fromEntries(
        nodes
          .filter((node) => node.position)
          .map((node) => [
            node.id,
            { x: node.position?.x ?? 0, y: node.position?.y ?? 0 },
          ]),
      ),
    },
  };
}

/** Drafts preserve unfinished work; execution checks belong to validation and review. */
export function draftFromGraph(
  name: string,
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): Definition {
  return definitionFromGraph(name, nodes, edges, true);
}

export interface PipelineDiagnostic {
  code:
    | "source"
    | "input"
    | "model"
    | "field"
    | "branch"
    | "cycle"
    | "connection"
    | "duplicate"
    | "join"
    | "blocks"
    | "transfers"
    | "reference"
    | "backend";
  nodeId?: string;
  field?: string;
  edgeIndex?: number;
}

/** One bounded traversal keeps diagnostics out of render-time graph walks. */
export function diagnoseGraph(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): PipelineDiagnostic[] {
  const issues: PipelineDiagnostic[] = [];
  if (!nodes.some((node) => node.data.stepType === "source"))
    issues.push({ code: "source" });
  const parents = new Set(edges.map((edge) => edge.target));
  for (const node of nodes) {
    const { stepType: kind, config } = node.data;
    if (
      kind !== "source" &&
      !parents.has(node.id) &&
      !["cron", "on_demand"].includes(String(config.params?.trigger))
    )
      issues.push({ code: "input", nodeId: node.id });
    if (kind === "inference" && !config.model && !config.capability_id)
      issues.push({ code: "model", nodeId: node.id, field: "model" });
    if (kind === "source") {
      for (const field of ["topic", "match_pattern", "schema_path"] as const)
        if (!config[field]?.trim())
          issues.push({ code: "field", nodeId: node.id, field });
    }
    if (kind === "pipeline" && !config.pipeline_ref)
      issues.push({ code: "reference", nodeId: node.id });
    if (kind === "route")
      for (const label of branchLabels(config)) {
        if (
          !label.trim() ||
          !edges.some(
            (edge) => edge.source === node.id && edge.sourceHandle === label,
          )
        )
          issues.push({ code: "branch", nodeId: node.id, field: label });
      }
  }
  try {
    validateConnections(nodes, edges);
  } catch (error) {
    const code = error instanceof Error ? error.message : "connection";
    if (
      [
        "cycle",
        "connection",
        "duplicate",
        "join",
        "blocks",
        "transfers",
        "branch",
      ].includes(code)
    ) {
      issues.push({ code: code as PipelineDiagnostic["code"] });
    }
  }
  return issues;
}

/** Produces a candidate only; callers must show removed links before applying it. */
export function proposeConnectionRepair<T extends GraphEdge>(
  nodes: readonly GraphNode[],
  edges: readonly T[],
): { edges: T[]; removed: T[] } {
  const accepted: T[] = [],
    removed: T[] = [];
  for (const edge of edges) {
    try {
      validateConnections(nodes, [...accepted, edge]);
      accepted.push(edge);
    } catch {
      removed.push(edge);
    }
  }
  return { edges: accepted, removed };
}

/** A settings edit must not collapse existing fan-in or shared route outcomes. */
export function replaceIncomingConnection(
  edges: readonly (GraphEdge & { id: string })[],
  target: string,
  source: string,
  label: string,
  union: boolean,
): Array<GraphEdge & { id: string }> {
  const current = edges.find((edge) => edge.target === target);
  const currentLabel =
    current?.sourceHandle === "record" ? "" : (current?.sourceHandle ?? "");
  if (
    (!current && !source) ||
    (current?.source === source && currentLabel === label)
  )
    return [...edges];
  const result = edges.filter(
    (edge) => edge.target !== target || (union && edge.id !== current?.id),
  );
  if (source)
    result.push({
      id: `connection:${source}:${target}:${label}`,
      source,
      target,
      sourceHandle: label || "record",
      targetHandle: "record",
    });
  return result;
}

/** A deterministic topological layout preserves fan-in without overlapping blocks. */
export function arrangeGraph(
  nodes: readonly GraphNode[],
  edges: readonly GraphEdge[],
): Record<string, { x: number; y: number }> {
  const degree = new Map(nodes.map((node) => [node.id, 0]));
  const depth = new Map(nodes.map((node) => [node.id, 0]));
  const children = new Map<string, string[]>();
  for (const edge of edges) {
    if (!degree.has(edge.source) || !degree.has(edge.target)) continue;
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    const next = children.get(edge.source) ?? [];
    next.push(edge.target);
    children.set(edge.source, next);
  }
  const ready = [...degree]
    .filter(([, value]) => value === 0)
    .map(([id]) => id);
  for (let index = 0; index < ready.length; index++) {
    const id = ready[index];
    if (!id) continue;
    for (const child of children.get(id) ?? []) {
      depth.set(
        child,
        Math.max(depth.get(child) ?? 0, (depth.get(id) ?? 0) + 1),
      );
      const remaining = (degree.get(child) ?? 0) - 1;
      degree.set(child, remaining);
      if (!remaining) ready.push(child);
    }
  }
  if (ready.length !== nodes.length) throw new Error("cycle");
  const rows = new Map<number, number>();
  return Object.fromEntries(
    nodes.map((node) => {
      const column = depth.get(node.id) ?? 0;
      const row = rows.get(column) ?? 0;
      rows.set(column, row + 1);
      return [node.id, { x: 40 + column * 320, y: 80 + row * 180 }];
    }),
  );
}
