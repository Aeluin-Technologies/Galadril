import type { StepKind } from "./pipeline";

/** Graph-only categorical colors pair white headers with contrast above 4.5:1. */
export const pipelinePalette = {
  input: { color: "#087F75", label: "pipeline.chrome.inputs" },
  processing: { color: "#65459B", label: "pipeline.chrome.processing" },
  control: { color: "#946200", label: "pipeline.chrome.control" },
  output: { color: "#235D97", label: "pipeline.chrome.outputs" },
} as const;
export function nodeCategory(kind: StepKind): keyof typeof pipelinePalette {
  if (kind === "source") return "input";
  if (kind === "route" || kind === "filter" || kind === "forward")
    return "control";
  if (kind === "sink" || kind === "pipeline") return "output";
  return "processing";
}
