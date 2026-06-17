import { ref } from "vue";
import type { Edge } from "@vue-flow/core";
import { stringify } from "yaml";

export const usePipeline = () => {
  const pipelineName = ref("");
  const pipelineNodes = ref<any[]>([
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
  const pipelineEdges = ref<Edge[]>([]);

  const history = ref<string[]>([]);
  const currentIndex = ref(-1);

  const initHistory = () => {
    if (history.value.length === 0) {
      const snapshot = JSON.stringify({
        nodes: pipelineNodes.value,
        edges: pipelineEdges.value,
      });
      history.value.push(snapshot);
      currentIndex.value = 0;
    }
  };

  const saveToHistory = () => {
    initHistory();
    const snapshot = JSON.stringify({
      nodes: pipelineNodes.value,
      edges: pipelineEdges.value,
    });

    if (
      currentIndex.value >= 0 &&
      history.value[currentIndex.value] === snapshot
    ) {
      return;
    }

    history.value = history.value.slice(0, currentIndex.value + 1);
    history.value.push(snapshot);
    currentIndex.value++;
  };

  const undo = () => {
    if (currentIndex.value > 0) {
      currentIndex.value--;
      const parsed = JSON.parse(history.value[currentIndex.value] || "");
      pipelineNodes.value = parsed.nodes;
      pipelineEdges.value = parsed.edges;
    }
  };

  const redo = () => {
    if (currentIndex.value < history.value.length - 1) {
      currentIndex.value++;
      const parsed = JSON.parse(history.value[currentIndex.value] || "");
      pipelineNodes.value = parsed.nodes;
      pipelineEdges.value = parsed.edges;
    }
  };

  const exportPipelineYaml = (flowObject: any) => {
    const sources: any[] = [];
    const pipeline: any[] = [];

    const sortedNodes = [...flowObject.nodes].sort((a, b) => {
      const aType = a.data?.stepType === "source" ? 0 : 1;
      const bType = b.data?.stepType === "source" ? 0 : 1;
      return aType - bType;
    });

    sortedNodes.forEach((node: any) => {
      const data = node.data;
      if (!data) return;

      const incomingEdges = flowObject.edges.filter(
        (e: any) => e.target === node.id,
      );
      const inputFrom = incomingEdges.map((e: any) => e.source);

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
