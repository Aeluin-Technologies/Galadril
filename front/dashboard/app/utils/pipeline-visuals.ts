import type { StepKind } from "./pipeline";

/** Graph-only categorical colors pair white headers with contrast above 4.5:1. */
export const pipelinePalette = {
  input: { color: "#16645F", label: "pipeline.chrome.inputs" },
  processing: { color: "#465C8B", label: "pipeline.chrome.processing" },
  control: { color: "#7A5B20", label: "pipeline.chrome.control" },
  output: { color: "#346579", label: "pipeline.chrome.outputs" },
} as const;
export function nodeCategory(kind: StepKind): keyof typeof pipelinePalette {
  if (kind === "source") return "input";
  if (kind === "route" || kind === "filter" || kind === "forward")
    return "control";
  if (kind === "sink" || kind === "pipeline") return "output";
  return "processing";
}
