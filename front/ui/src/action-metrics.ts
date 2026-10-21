import { metrics } from "@opentelemetry/api";

const meter = metrics.getMeter("@galadril/ui");
const overlayFailures = meter.createCounter("ui.overlay.failure.count", {
  description: "Overlay positioning failures without user data",
});

/** Lets the host observe positioning failures without exposing tooltip content. */
export function recordOverlayFailure(): void {
  overlayFailures.add(1, { overlay: "tooltip" });
}
const actions = meter.createCounter("ui.action.count", {
  description: "Completed UI actions by bounded outcome",
});
const duration = meter.createHistogram("ui.action.duration", {
  unit: "ms",
  description: "UI action latency",
});

/** Records bounded outcomes through the host's OTLP provider without user data. */
export function recordAction(
  start: number,
  outcome: "success" | "failure",
): void {
  const attributes = { outcome };
  actions.add(1, attributes);
  duration.record(performance.now() - start, attributes);
}
