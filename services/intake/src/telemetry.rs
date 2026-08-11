//! OTLP tracing and W3C propagation for the intake process.

use std::collections::HashMap;

use anyhow::{Context as _, Result};
use opentelemetry::trace::{TraceContextExt as _, TracerProvider as _};
use opentelemetry::{Context, global};
use opentelemetry_otlp::SpanExporter;
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::propagation::TraceContextPropagator;
use opentelemetry_sdk::trace::SdkTracerProvider;
use tracing_opentelemetry::OpenTelemetrySpanExt as _;
use tracing_subscriber::prelude::*;
use tracing_subscriber::{EnvFilter, fmt};

/// Initializes structured JSON logs and the OTLP batch span exporter.
pub fn initialize(service_name: &'static str) -> Result<SdkTracerProvider> {
    global::set_text_map_propagator(TraceContextPropagator::new());

    let exporter = SpanExporter::builder()
        .with_tonic()
        .build()
        .context("failed to build OTLP span exporter")?;
    let resource = Resource::builder().with_service_name(service_name).build();
    let provider = SdkTracerProvider::builder()
        .with_resource(resource)
        .with_batch_exporter(exporter)
        .build();
    let tracer = provider.tracer(service_name);
    global::set_tracer_provider(provider.clone());

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
        .with(
            fmt::layer()
                .json()
                .with_current_span(true)
                .with_span_list(true),
        )
        .with(tracing_opentelemetry::layer().with_tracer(tracer))
        .try_init()
        .context("failed to initialize intake telemetry subscriber")?;
    Ok(provider)
}

/// Serializes an OpenTelemetry context into a Kafka-safe W3C carrier.
#[inline]
pub fn w3c_carrier(context: &Context) -> HashMap<String, String> {
    let mut carrier = HashMap::with_capacity(2);
    global::get_text_map_propagator(|propagator| {
        propagator.inject_context(context, &mut carrier);
    });
    carrier
}

/// Returns W3C headers from the active `tracing` span.
#[inline]
pub fn current_w3c_carrier() -> HashMap<String, String> {
    w3c_carrier(&tracing::Span::current().context())
}

/// Records active OTel identifiers onto the current structured log span.
#[inline]
pub fn record_current_trace_identifiers() {
    let tracing_span = tracing::Span::current();
    let otel_context = tracing_span.context();
    let otel_span = otel_context.span();
    let span_context = otel_span.span_context();
    if !span_context.is_valid() {
        return;
    }
    tracing_span.record("trace_id", span_context.trace_id().to_string());
    tracing_span.record("span_id", span_context.span_id().to_string());
}

#[cfg(test)]
mod tests {
    use opentelemetry::trace::{
        SpanContext, SpanId, TraceFlags, TraceId, TraceState,
    };

    use super::*;

    #[test]
    fn carrier_preserves_exact_trace_and_span_identifiers() -> Result<()> {
        global::set_text_map_propagator(TraceContextPropagator::new());
        let trace_id = TraceId::from_hex("4bf92f3577b34da6a3ce929d0e0e4736")?;
        let span_id = SpanId::from_hex("00f067aa0ba902b7")?;
        let span_context = SpanContext::new(
            trace_id,
            span_id,
            TraceFlags::SAMPLED,
            true,
            TraceState::default(),
        );
        let context = Context::new().with_remote_span_context(span_context);

        let carrier = w3c_carrier(&context);

        assert_eq!(
            carrier.get("traceparent").map(String::as_str),
            Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        );
        Ok(())
    }
}
