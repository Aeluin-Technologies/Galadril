//! Role-aware OTLP/gRPC telemetry shared by Galadril binaries and libraries.
#![deny(unsafe_code, missing_docs)]

use std::env;
use std::time::Duration;

use anyhow::{Context as _, Result};
use opentelemetry::metrics::Meter;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry::{InstrumentationScope, KeyValue, Value, global};
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

/// Selects whether telemetry owns exporters or contributes library
/// instruments.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TelemetryRole {
    /// Installs the process-wide subscriber, providers, and OTLP exporters.
    Binary,
    /// Uses the process-wide providers installed by the hosting binary.
    Library,
}

/// Typed telemetry configuration for executable and library runtimes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TelemetryConfig {
    /// Configures process-wide telemetry for an executable.
    Binary {
        /// OpenTelemetry service name.
        name: &'static str,
        /// Build or package version of the executable.
        version: &'static str,
    },
    /// Configures an instrumentation scope for a library crate.
    Library {
        /// OpenTelemetry instrumentation scope name.
        name: &'static str,
        /// Build or package version of the library.
        version: &'static str,
    },
}

impl TelemetryConfig {
    /// Returns the runtime role without configuring global telemetry state.
    pub const fn role(&self) -> TelemetryRole {
        match self {
            Self::Binary { .. } => TelemetryRole::Binary,
            Self::Library { .. } => TelemetryRole::Library,
        }
    }
}

/// Configures telemetry through a single role-aware entry point.
pub trait ConfigureTelemetry {
    /// Applies this configuration and returns its scoped telemetry handle.
    fn configure(self) -> Result<Telemetry>;
}

/// Scoped meter and optional process-owned OpenTelemetry providers.
pub struct Telemetry {
    role: TelemetryRole,
    scope: InstrumentationScope,
    meter: Meter,
    providers: Option<TelemetryProviders>,
}

impl Telemetry {
    /// Returns the configured runtime role.
    pub const fn role(&self) -> TelemetryRole {
        self.role
    }

    /// Returns the OpenTelemetry identity attached to emitted instruments.
    pub const fn scope(&self) -> &InstrumentationScope {
        &self.scope
    }

    /// Returns a meter bound to this binary or library instrumentation scope.
    pub fn meter(&self) -> Meter {
        self.meter.clone()
    }

    /// Flushes owned exporters; library handles complete without global work.
    pub fn shutdown(self) -> Result<()> {
        match self.providers {
            Some(providers) => providers.shutdown(),
            None => Ok(()),
        }
    }
}

struct TelemetryProviders {
    logger_provider: SdkLoggerProvider,
    meter_provider: SdkMeterProvider,
    tracer_provider: SdkTracerProvider,
}

impl TelemetryProviders {
    fn shutdown(self) -> Result<()> {
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

impl ConfigureTelemetry for TelemetryConfig {
    fn configure(self) -> Result<Telemetry> {
        let role = self.role();
        let (name, version) = match self {
            Self::Binary { name, version } |
            Self::Library { name, version } => (name, version),
        };
        let scope = library_scope(name, version);
        let providers = match self {
            Self::Binary { .. } => Some(initialize_binary(name, version)?),
            Self::Library { .. } => None,
        };
        let meter = global::meter_with_scope(scope.clone());
        Ok(Telemetry {
            role,
            scope,
            meter,
            providers,
        })
    }
}

/// Installs the common OTLP/gRPC log, metric, trace, and W3C propagation
/// stack.
fn initialize_binary(
    service_name: &'static str,
    service_version: &'static str,
) -> Result<TelemetryProviders> {
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

    Ok(TelemetryProviders {
        logger_provider,
        meter_provider,
        tracer_provider,
    })
}

fn library_scope(
    library_name: &'static str,
    library_version: &'static str,
) -> InstrumentationScope {
    InstrumentationScope::builder(library_name)
        .with_version(library_version)
        .build()
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

    #[test]
    fn library_scope_contains_crate_identity() {
        let scope = library_scope("galadril.test-library", "4.2.0");

        assert_eq!(scope.name(), "galadril.test-library");
        assert_eq!(scope.version(), Some("4.2.0"));
    }

    #[test]
    fn library_configuration_exposes_a_scoped_meter_without_exporters()
    -> Result<()> {
        let telemetry = TelemetryConfig::Library {
            name: "galadril.test-library",
            version: "4.2.0",
        }
        .configure()?;

        assert_eq!(telemetry.role(), TelemetryRole::Library);
        assert!(telemetry.providers.is_none());
        assert_eq!(telemetry.scope().name(), "galadril.test-library");
        assert_eq!(telemetry.scope().version(), Some("4.2.0"));
        Ok(())
    }

    #[test]
    fn configuration_variants_report_their_runtime_role() {
        assert_eq!(
            TelemetryConfig::Binary {
                name: "galadril.test-binary",
                version: "1.0.0",
            }
            .role(),
            TelemetryRole::Binary
        );
        assert_eq!(
            TelemetryConfig::Library {
                name: "galadril.test-library",
                version: "1.0.0",
            }
            .role(),
            TelemetryRole::Library
        );
    }
}
