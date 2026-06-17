import { ref } from "vue";
import type { Edge } from "@vue-flow/core";

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

  // Systèmes d'historique
  const historyStack = ref<string[]>([]);
  const redoStack = ref<string[]>([]);

  const saveToHistory = () => {
    const snapshot = JSON.stringify({
      nodes: pipelineNodes.value,
      edges: pipelineEdges.value,
    });
    if (
      historyStack.value.length === 0 ||
      historyStack.value[historyStack.value.length - 1] !== snapshot
    ) {
      historyStack.value.push(snapshot);
      redoStack.value = [];
    }
  };

  const undo = () => {
    if (historyStack.value.length > 0) {
      const currentSnapshot = JSON.stringify({
        nodes: pipelineNodes.value,
        edges: pipelineEdges.value,
      });
      redoStack.value.push(currentSnapshot);
      const previousState = historyStack.value.pop();
      if (previousState) {
        const parsed = JSON.parse(previousState);
        pipelineNodes.value = parsed.nodes;
        pipelineEdges.value = parsed.edges;
      }
    }
  };

  const redo = () => {
    if (redoStack.value.length > 0) {
      const nextState = redoStack.value.pop();
      if (nextState) {
        const currentSnapshot = JSON.stringify({
          nodes: pipelineNodes.value,
          edges: pipelineEdges.value,
        });
        historyStack.value.push(currentSnapshot);
        const parsed = JSON.parse(nextState);
        pipelineNodes.value = parsed.nodes;
        pipelineEdges.value = parsed.edges;
      }
    }
  };

  // Convertisseur YAML
  const jsonToYaml = (obj: any): string => {
    let yaml = "";
    if (obj.sources?.length) {
      yaml += "sources:\n";
      obj.sources.forEach((s: any) => {
        yaml += `  - id: ${s.id}\n    topic: ${s.topic || ""}\n    match_pattern: "${s.match_pattern || ""}"\n    schema_path: ${s.schema_path || ""}\n`;
      });
    }
    if (obj.pipeline?.length) {
      yaml += "pipeline:\n";
      obj.pipeline.forEach((p: any) => {
        yaml += `  - step: ${p.step}\n    type: ${p.type}\n    input_from:\n`;
        if (p.input_from?.length) {
          p.input_from.forEach((inf: string) => {
            yaml += `      - ${inf}\n`;
          });
        } else {
          yaml += `      - []\n`;
        }
        if (p.connector) yaml += `    connector: ${p.connector}\n`;
        if (p.model) yaml += `    model: ${p.model}\n`;
        if (p.params && Object.keys(p.params).length) {
          yaml += `    params:\n`;
          Object.entries(p.params).forEach(([k, v]) => {
            yaml += `      ${k}: ${v}\n`;
          });
        }
      });
    }
    return yaml;
  };

  const exportPipelineYaml = (flowObject: any) => {
    const sources: any[] = [];
    const pipeline: any[] = [];

    flowObject.nodes.forEach((node: any) => {
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

    const cleanYaml = jsonToYaml({ sources, pipeline });
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
