import { blockKind, pluginConfig } from "~/utils/plugin-blocks";
import type { PluginBlock } from "~/utils/plugin-blocks";
import { stringify } from "yaml";
import type { Node, Edge, Connection } from "@vue-flow/core";
import {
  branchLabels,
  copyConfig,
  definitionFromGraph,
  validateConnections,
} from "~/utils/pipeline";
import type {
  NodeConfig,
  StepKind,
  PipelineArtifact,
  Definition,
} from "~/utils/pipeline";

export interface EditorNodeData extends Record<string, unknown> {
  stepType: StepKind;
  config: NodeConfig;
  title?: string;
}
export type EditorNode = Node<EditorNodeData> & { data: EditorNodeData };

/** Editor history contains authored data only; expanded children remain read-only. */
export function usePipelineEditor() {
  const catalog = usePipelineCatalog();
  const { t } = useI18n();
  const name = ref(t("pipeline.composition.new_pipeline"));
  const nodes = shallowRef<EditorNode[]>([
    {
      id: "input",
      type: "custom",
      position: { x: 40, y: 160 },
      data: {
        stepType: "source",
        config: {
          topic: "raw",
          match_pattern: ".*",
          schema_path: "schemas/avro/text.avsc",
          parser: "text",
        },
      },
    },
  ]);
  const edges = shallowRef<Edge[]>([]);
  const failure = ref(false);
  const validated = ref(false);
  const expandedBlocks = ref<number>();
  const cursor = ref(0);
  function snapshot() {
    return {
      nodes: nodes.value.map((node) => ({
        id: node.id,
        type: "custom",
        position: { ...node.position },
        data: {
          stepType: node.data.stepType,
          config: copyConfig(node.data.config),
          title: node.data.title,
        },
      })),
      edges: edges.value.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        sourceHandle: edge.sourceHandle,
      })),
    };
  }
  const history = [snapshot()];
  function remember() {
    history.splice(cursor.value + 1);
    history.push(snapshot());
    if (history.length > 50) history.shift();
    cursor.value = history.length - 1;
    validated.value = false;
    expandedBlocks.value = undefined;
    failure.value = false;
  }
  function restore(index: number) {
    const value = history[index];
    if (!value) return;
    nodes.value = value.nodes.map((node) => ({
      ...node,
      position: { ...node.position },
      data: { ...node.data, config: copyConfig(node.data.config) },
    }));
    edges.value = value.edges.map((edge) => ({ ...edge }));
    cursor.value = index;
    validated.value = false;
    expandedBlocks.value = undefined;
  }
  function add(
    kind: StepKind,
    artifact?: PipelineArtifact,
    inputSource?: string,
    plugin?: PluginBlock,
  ) {
    const config: NodeConfig =
      kind === "source"
        ? {
            topic: "raw",
            match_pattern: ".*",
            schema_path: "schemas/avro/text.avsc",
            parser: "text",
          }
        : { params: {} };
    if (kind === "route")
      Object.assign(config, {
        routing: "regex",
        branch_labels: ["match", "other"],
        params: {
          field: "record.raw_payload.content",
          pattern: "appeal",
          match_branch: "match",
          default_branch: "other",
        },
      });
    if (kind === "inference")
      config.model =
        "galadril_inference.models.embedding.qwen3.Qwen3EmbeddingModel";
    if (artifact) {
      const input = artifact.definition.sources[0];
      if (!input) throw new Error("PIPELINE_UNAVAILABLE");
      config.pipeline_ref = {
        pipeline_id: artifact.pipeline_id,
        revision_id: artifact.head_revision_id,
        input_source: inputSource ?? input.id,
      };
    }
    if (plugin) {
      delete config.model;
      Object.assign(config, pluginConfig(plugin));
    }
    const candidate: EditorNode = {
      id: `${kind}_${crypto.randomUUID().slice(0, 8)}`,
      type: "custom",
      position: {
        x: 340 + (nodes.value.length % 3) * 300,
        y: 70 + Math.floor(nodes.value.length / 3) * 180,
      },
      data: { stepType: kind, config, title: plugin?.name ?? artifact?.name },
    };
    validateConnections([...nodes.value, candidate], edges.value);
    nodes.value = [...nodes.value, candidate];
    remember();
  }
  function addPlugin(plugin: PluginBlock) {
    add(blockKind(plugin.stepType), undefined, undefined, plugin);
  }
  function remove(id: string) {
    nodes.value = nodes.value.filter((node) => node.id !== id);
    edges.value = edges.value.filter(
      (edge) => edge.source !== id && edge.target !== id,
    );
    remember();
  }
  function connect(connection: Connection) {
    try {
      const candidate: Edge = {
        id: crypto.randomUUID(),
        source: connection.source,
        target: connection.target,
        sourceHandle: connection.sourceHandle,
      };
      validateConnections(nodes.value, [...edges.value, candidate]);
      edges.value = [...edges.value, candidate];
      remember();
    } catch {
      failure.value = true;
    }
  }
  function configure(
    id: string,
    config: NodeConfig,
    upstream: string,
    label: string,
  ) {
    const current = nodes.value.find((node) => node.id === id);
    if (!current) return;
    const previous = branchLabels(current.data.config),
      next = branchLabels(config);
    const candidateNodes = nodes.value.map((node) =>
      node.id === id
        ? { ...node, data: { ...node.data, config: copyConfig(config) } }
        : node,
    );
    let candidateEdges = edges.value
      .filter((edge) => edge.target !== id)
      .flatMap((edge) => {
        if (edge.source !== id || current.data.stepType !== "route")
          return [edge];
        const replacement = next[previous.indexOf(edge.sourceHandle ?? "")];
        return replacement ? [{ ...edge, sourceHandle: replacement }] : [];
      });
    if (upstream)
      candidateEdges.push({
        id: crypto.randomUUID(),
        source: upstream,
        target: id,
        sourceHandle: label || undefined,
      });
    validateConnections(candidateNodes, candidateEdges);
    nodes.value = candidateNodes;
    edges.value = candidateEdges;
    remember();
  }
  async function validate(): Promise<Definition> {
    failure.value = false;
    validated.value = false;
    try {
      const definition = definitionFromGraph(
        name.value,
        nodes.value,
        edges.value,
      );
      const result = await catalog.validate("draft", definition);
      expandedBlocks.value = result.blocks;
      validated.value = true;
      return definition;
    } catch (error) {
      failure.value = true;
      throw error;
    }
  }
  async function download() {
    const definition = await validate();
    const url = URL.createObjectURL(
      new Blob([stringify(definition)], { type: "application/yaml" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${name.value.replace(/[^a-zA-Z0-9_-]/g, "_") || "pipeline"}.yaml`;
    anchor.click();
    URL.revokeObjectURL(url);
  }
  watch(name, () => {
    validated.value = false;
  });
  return {
    name,
    nodes,
    edges,
    failure,
    validated,
    expandedBlocks,
    add,
    addPlugin,
    remove,
    connect,
    configure,
    remember,
    validate,
    download,
    undo: () => restore(cursor.value - 1),
    redo: () => restore(cursor.value + 1),
    canUndo: computed(() => cursor.value > 0),
    canRedo: computed(() => cursor.value < history.length - 1),
  };
}
