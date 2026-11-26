import {
  MeterProvider,
  PeriodicExportingMetricReader,
  AggregationType,
  createAllowListAttributesProcessor,
} from "@opentelemetry/sdk-metrics";
import { resourceFromAttributes } from "@opentelemetry/resources";
import type { PushMetricExporter } from "@opentelemetry/sdk-metrics";
import {
  METRIC_AUTH_ATTEMPTS,
  authAttemptsAttributeKeys,
} from "./telemetry-contract.ts";

/** Unapproved instruments must never fall back to the SDK's default aggregation. */
class AggregateMetricReader extends PeriodicExportingMetricReader {
  override selectAggregation() {
    return { type: AggregationType.DROP };
  }
}

/** Discards unapproved instruments and identity attributes before aggregation. */
export function createAggregateMetricsProvider(exporter: PushMetricExporter) {
  return new MeterProvider({
    resource: resourceFromAttributes({ "service.name": "galadril-studio" }),
    readers: [
      new AggregateMetricReader({ exporter, exportIntervalMillis: 60000 }),
    ],
    views: [
      {
        instrumentName: METRIC_AUTH_ATTEMPTS,
        aggregation: { type: AggregationType.SUM },
        attributesProcessors: [
          createAllowListAttributesProcessor([...authAttemptsAttributeKeys]),
        ],
        aggregationCardinalityLimit: 32,
      },
    ],
  });
}
