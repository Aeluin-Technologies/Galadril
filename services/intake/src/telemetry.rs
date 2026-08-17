//! W3C propagation helpers for the shared Galadril telemetry runtime.

use opentelemetry::propagation::Injector;
use opentelemetry::trace::TraceContextExt as _;
use opentelemetry::{Context, global};
use tracing_opentelemetry::OpenTelemetrySpanExt as _;

/// Allocation-minimal carrier for the two W3C Trace Context headers.
#[derive(Debug, Default)]
pub struct TraceCarrier {
    traceparent: Option<String>,
    tracestate: Option<String>,
}

impl TraceCarrier {
    /// Returns a propagated header without allocating another map.
    #[inline]
    pub fn get(&self, key: &str) -> Option<&str> {
        match key {
            "traceparent" => self.traceparent.as_deref(),
            "tracestate" => self.tracestate.as_deref(),
            _ => None,
        }
    }

    /// Returns the fixed-capacity carrier entries for zero-copy iteration.
    #[inline]
    pub fn entries(&self) -> [Option<(&'static str, &str)>; 2] {
        [
            self.traceparent
                .as_deref()
                .map(|value| ("traceparent", value)),
            self.tracestate
                .as_deref()
                .map(|value| ("tracestate", value)),
        ]
    }
}

impl Injector for TraceCarrier {
    fn set(&mut self, key: &str, value: String) {
        match key {
            "traceparent" => self.traceparent = Some(value),
            "tracestate" => self.tracestate = Some(value),
            _ => {},
        }
    }
}

/// Serializes an OpenTelemetry context into a Kafka-safe W3C carrier.
#[inline]
pub fn w3c_carrier(context: &Context) -> TraceCarrier {
    let mut carrier = TraceCarrier::default();
    global::get_text_map_propagator(|propagator| {
        propagator.inject_context(context, &mut carrier);
    });
    carrier
}

/// Returns W3C headers from the active `tracing` span.
#[inline]
pub fn current_w3c_carrier() -> TraceCarrier {
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
    use anyhow::Result;
    use opentelemetry::trace::{
        SpanContext, SpanId, TraceFlags, TraceId, TraceState,
    };
    use opentelemetry_sdk::propagation::TraceContextPropagator;

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
            carrier.get("traceparent"),
            Some("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        );
        Ok(())
    }
}
