import { metrics } from "@opentelemetry/api";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-http";
import { createAggregateMetricsProvider } from "../auth/metrics.ts";

export default defineNitroPlugin((nitro) => {
  if (process.env.NUXT_AGGREGATE_METRICS_ENABLED !== "true") return;
  const endpoint = new URL(process.env.NUXT_METRICS_OTLP_ENDPOINT ?? "");
  if (
    !["http:", "https:"].includes(endpoint.protocol) ||
    endpoint.username ||
    endpoint.password ||
    endpoint.search ||
    endpoint.hash
  )
    throw new Error("Invalid private metrics collector");
  // Per-request exemplars and host/process resource detection would identify users or instances.
  process.env.OTEL_METRICS_EXEMPLAR_FILTER = "always_off";
  const provider = createAggregateMetricsProvider(
    new OTLPMetricExporter({ url: endpoint.href }),
  );
  metrics.setGlobalMeterProvider(provider);
  nitro.hooks.hook("close", () => provider.shutdown());
});
