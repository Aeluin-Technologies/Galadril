import {
  MeterProvider,
  PeriodicExportingMetricReader,
  AggregationType,
  createAllowListAttributesProcessor,
} from "@opentelemetry/sdk-metrics";
import { resourceFromAttributes } from "@opentelemetry/resources";

/** Unapproved instruments must never fall back to the SDK's default aggregation. */
class AggregateMetricReader extends PeriodicExportingMetricReader {
  selectAggregation() {
    return { type: AggregationType.DROP };
  }
}

/** Discards unapproved instruments and identity attributes before aggregation. */
export function createAggregateMetricsProvider(exporter) {
  return new MeterProvider({
    resource: resourceFromAttributes({ "service.name": "galadril-studio" }),
    readers: [
      new AggregateMetricReader({ exporter, exportIntervalMillis: 60000 }),
    ],
    views: [
      {
        instrumentName: "galadril.auth.attempts",
        aggregation: { type: AggregationType.SUM },
        attributesProcessors: [
          createAllowListAttributesProcessor(["operation", "outcome"]),
        ],
        aggregationCardinalityLimit: 32,
      },
    ],
  });
}
