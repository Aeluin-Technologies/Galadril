import { ref, shallowRef, toRaw } from "vue";
import type {
  GraphEdge,
  SourceDefinition,
  StepDefinition,
} from "../utils/pipeline.ts";
import { stringify } from "yaml";

interface FlowNode {
  id: string;
  type: string;
  position: { x: number; y: number };
  data?: {
    stepType: string;
    config?: Partial<
      Pick<SourceDefinition, "topic" | "match_pattern" | "schema_path">
    > &
      Partial<Pick<StepDefinition, "model" | "params">> & {
        connector?: string;
      };
  };
}

interface FlowSnapshot {
  nodes: FlowNode[];
  edges: GraphEdge[];
}

export const usePipeline = () => {
  const pipelineName = ref("");
  const pipelineNodes = ref<FlowNode[]>([
    {
      id: "source_4229",
      type: "custom",
      position: { x: 100, y: 150 },
      data: {
        stepType: "source",
        config: {
          topic: "raw-images",
          match_pattern: "^images(/|%2F)",
          schema_path: "schemas/avro/image.avsc",
          params: {},
        },
      },
    },
  ]);
  const pipelineEdges = ref<GraphEdge[]>([]);

  const history = shallowRef<FlowSnapshot[]>([]);
  const currentIndex = ref(-1);

  const capture = (): FlowSnapshot =>
    structuredClone({
      nodes: toRaw(pipelineNodes.value),
      edges: toRaw(pipelineEdges.value),
    });

  const initHistory = () => {
    if (history.value.length === 0) {
      history.value.push(capture());
      currentIndex.value = 0;
    }
  };

  const saveToHistory = () => {
    initHistory();
    const snapshot = capture();

    if (
      currentIndex.value >= 0 &&
      JSON.stringify(history.value[currentIndex.value]) ===
        JSON.stringify(snapshot)
    ) {
      return;
    }

    history.value = history.value.slice(0, currentIndex.value + 1);
    history.value.push(snapshot);
    currentIndex.value++;
  };

  const restore = () => {
    const snapshot = history.value[currentIndex.value];
    if (!snapshot) throw new Error("Missing pipeline history snapshot");

    const graph = structuredClone(snapshot);
    pipelineNodes.value = graph.nodes;
    pipelineEdges.value = graph.edges;
  };

  const undo = () => {
    if (currentIndex.value > 0) {
      currentIndex.value--;
      restore();
    }
  };

  const redo = () => {
    if (currentIndex.value < history.value.length - 1) {
      currentIndex.value++;
      restore();
    }
  };

  const exportPipelineYaml = (flowObject: FlowSnapshot) => {
    const sources: SourceDefinition[] = [];
    const pipeline: (StepDefinition & { connector?: string })[] = [];

    const sortedNodes = [...flowObject.nodes].sort((a, b) => {
      const aType = a.data?.stepType === "source" ? 0 : 1;
      const bType = b.data?.stepType === "source" ? 0 : 1;
      return aType - bType;
    });

    sortedNodes.forEach((node) => {
      const data = node.data;
      if (!data) return;

      const incomingEdges = flowObject.edges.filter(
        (edge) => edge.target === node.id,
      );
      const inputFrom = incomingEdges.map((edge) => edge.source);

      if (data.stepType === "source") {
        sources.push({
          id: node.id,
          topic: data.config?.topic || "",
          match_pattern: data.config?.match_pattern || "",
          schema_path: data.config?.schema_path || "",
        });
      } else {
        pipeline.push({
          step: node.id,
          type: data.stepType,
          input_from: inputFrom,
          ...(data.config?.connector && { connector: data.config.connector }),
          ...(data.config?.model && { model: data.config.model }),
          params: data.config?.params || {},
        });
      }
    });

    const cleanYaml = stringify({ sources, pipeline });

    const dataStr =
      "data:text/yaml;charset=utf-8," + encodeURIComponent(cleanYaml);
    const downloadAnchor = document.createElement("a");
    downloadAnchor.setAttribute("href", dataStr);
    downloadAnchor.setAttribute(
      "download",
      `${pipelineName.value || "untitled_pipeline"}.yaml`,
    );
    document.body.appendChild(downloadAnchor);
    downloadAnchor.click();
    downloadAnchor.remove();
  };

  initHistory();

  return {
    pipelineName,
    pipelineNodes,
    pipelineEdges,
    saveToHistory,
    undo,
    redo,
    exportPipelineYaml,
  };
};
