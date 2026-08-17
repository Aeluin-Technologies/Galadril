//! Non-blocking OTLP/gRPC telemetry bootstrap shared by Galadril binaries.
#![deny(unsafe_code, missing_docs)]

use std::env;
use std::time::Duration;

use anyhow::{Context as _, Result};
use opentelemetry::trace::TracerProvider as _;
use opentelemetry::{KeyValue, Value, global};
use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
use opentelemetry_otlp::{LogExporter, MetricExporter, SpanExporter};
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::logs::{BatchLogProcessor, SdkLoggerProvider};
use opentelemetry_sdk::metrics::{PeriodicReader, SdkMeterProvider};
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::{BatchSpanProcessor, SdkTracerProvider};
use tracing_subscriber::EnvFilter;
use tracing_subscriber::layer::SubscriberExt as _;
use tracing_subscriber::util::SubscriberInitExt as _;

const DEFAULT_ENVIRONMENT: &str = "development";
const EXPORT_INTERVAL: Duration = Duration::from_secs(15);
const QUEUE_CAPACITY: usize = 2_048;
const EXPORT_BATCH_SIZE: usize = 512;

/// Owns every OpenTelemetry provider so exporters can be flushed on shutdown.
pub struct TelemetryGuard {
    logger_provider: SdkLoggerProvider,
    meter_provider: SdkMeterProvider,
    tracer_provider: SdkTracerProvider,
}

impl TelemetryGuard {
    /// Flushes and terminates background exporters without blocking request
    /// paths.
    pub fn shutdown(self) -> Result<()> {
        // Evaluate every shutdown before returning an error so one failed
        // exporter cannot prevent the remaining queues from being drained.
        let trace_result = self
            .tracer_provider
            .shutdown()
            .context("failed to shut down trace exporter");
        let metric_result = self
            .meter_provider
            .shutdown()
            .context("failed to shut down metric exporter");
        let log_result = self
            .logger_provider
            .shutdown()
            .context("failed to shut down log exporter");

        trace_result.and(metric_result).and(log_result)
    }
}

/// Installs the common OTLP/gRPC log, metric, trace, and W3C propagation
/// stack.
pub fn initialize(
    service_name: &'static str,
    service_version: &'static str,
) -> Result<TelemetryGuard> {
    global::set_text_map_propagator(TraceContextPropagator::new());
    let resource = resource(service_name, service_version);

    // Each SDK processor owns a bounded queue and dedicated exporter worker.
    let span_exporter = SpanExporter::builder()
        .with_tonic()
        .build()
        .context("failed to build OTLP/gRPC span exporter")?;
    let span_processor = BatchSpanProcessor::builder(span_exporter)
        .with_batch_config(
            opentelemetry_sdk::trace::BatchConfigBuilder::default()
                .with_max_queue_size(QUEUE_CAPACITY)
                .with_max_export_batch_size(EXPORT_BATCH_SIZE)
                .with_scheduled_delay(Duration::from_secs(5))
                .build(),
        )
        .build();
    let tracer_provider = SdkTracerProvider::builder()
        .with_resource(resource.clone())
        .with_span_processor(span_processor)
        .build();
    let tracer = tracer_provider.tracer(service_name);
    global::set_tracer_provider(tracer_provider.clone());

    let metric_exporter = MetricExporter::builder()
        .with_tonic()
        .build()
        .context("failed to build OTLP/gRPC metric exporter")?;
    let metric_reader = PeriodicReader::builder(metric_exporter)
        .with_interval(EXPORT_INTERVAL)
        .build();
    let meter_provider = SdkMeterProvider::builder()
        .with_resource(resource.clone())
        .with_reader(metric_reader)
        .build();
    global::set_meter_provider(meter_provider.clone());

    let log_exporter = LogExporter::builder()
        .with_tonic()
        .build()
        .context("failed to build OTLP/gRPC log exporter")?;
    let log_processor = BatchLogProcessor::builder(log_exporter)
        .with_batch_config(
            opentelemetry_sdk::logs::BatchConfigBuilder::default()
                .with_max_queue_size(QUEUE_CAPACITY)
                .with_max_export_batch_size(EXPORT_BATCH_SIZE)
                .with_scheduled_delay(Duration::from_secs(5))
                .build(),
        )
        .build();
    let logger_provider = SdkLoggerProvider::builder()
        .with_resource(resource)
        .with_log_processor(log_processor)
        .build();

    let level = if cfg!(debug_assertions) {
        "debug"
    } else {
        "info"
    };
    tracing_subscriber::registry()
        .with(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new(level)),
        )
        .with(OpenTelemetryTracingBridge::new(&logger_provider))
        .with(tracing_opentelemetry::layer().with_tracer(tracer))
        .try_init()
        .context("failed to install telemetry subscriber")?;

    Ok(TelemetryGuard {
        logger_provider,
        meter_provider,
        tracer_provider,
    })
}

fn resource(
    service_name: &'static str,
    service_version: &'static str,
) -> Resource {
    let environment = env::var("DEPLOYMENT_ENVIRONMENT")
        .unwrap_or_else(|_| DEFAULT_ENVIRONMENT.to_owned())
        .to_lowercase();
    let instance_id = env::var("HOSTNAME")
        .unwrap_or_else(|_| "unknown-host".to_owned())
        .to_lowercase();
    Resource::builder()
        .with_service_name(service_name)
        .with_attributes([
            KeyValue::new("service.version", service_version),
            KeyValue::new("deployment.environment", environment),
            KeyValue::new("service.instance.id", instance_id),
            KeyValue::new("telemetry.sdk.language", Value::from("rust")),
        ])
        .build()
}

#[cfg(test)]
mod tests {
    use opentelemetry::Key;

    use super::*;

    #[test]
    fn resource_contains_harmonized_required_attributes() {
        let resource = resource("galadril-test", "1.2.3");

        assert_eq!(
            resource.get(&Key::new("service.name")),
            Some(Value::from("galadril-test"))
        );
        assert_eq!(
            resource.get(&Key::new("service.version")),
            Some(Value::from("1.2.3"))
        );
        assert!(resource.get(&Key::new("deployment.environment")).is_some());
    }
}
