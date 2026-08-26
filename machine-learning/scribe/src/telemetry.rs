//! Extractable runtime metrics and OpenTelemetry instruments for Scribe.

use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use anyhow::Result;
use galadril_telemetry::{
    ConfigureTelemetry as _, Telemetry, TelemetryConfig,
};
use opentelemetry::KeyValue;
use opentelemetry::metrics::{Counter, Gauge, Histogram};
use serde::{Deserialize, Serialize};

/// Point-in-time, allocation-free Scribe runtime metrics.
#[derive(
    Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize,
)]
pub struct ScribeMetricsSnapshot {
    /// Model-reported output tokens, with streamed fragments as a fallback.
    pub generated_tokens: u64,
    /// Messages that reached an atomically committed terminal state.
    pub completed_messages: u64,
    /// Messages that terminated without mutating conversation history.
    pub failed_messages: u64,
    /// Conversation sessions currently retained by the engine.
    pub active_sessions: u64,
    /// Generations currently holding a session execution slot.
    pub active_generations: u64,
    /// Independently addressable models loaded by the engine.
    pub loaded_models: u64,
    /// Model-requested tool calls that reached a terminal state.
    pub tool_calls: u64,
    /// Tool calls that returned an error.
    pub failed_tool_calls: u64,
    /// Terminal notifications that could not reach the persistence consumer.
    pub completion_delivery_failures: u64,
}

pub(crate) struct ScribeMetrics {
    _telemetry: Telemetry,
    generations: Counter<u64>,
    generation_duration: Histogram<f64>,
    generated_token_counter: Counter<u64>,
    active_session_gauge: Gauge<u64>,
    active_generation_gauge: Gauge<u64>,
    loaded_model_gauge: Gauge<u64>,
    tool_call_counter: Counter<u64>,
    tool_duration: Histogram<f64>,
    completion_delivery_failure_counter: Counter<u64>,
    generated_tokens: AtomicU64,
    completed_messages: AtomicU64,
    failed_messages: AtomicU64,
    active_sessions: AtomicU64,
    active_generations: AtomicU64,
    loaded_models: u64,
    tool_calls: AtomicU64,
    failed_tool_calls: AtomicU64,
    completion_delivery_failures: AtomicU64,
}

/// Terminal result labels shared by traces, logs, and metrics.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum OperationOutcome {
    Success,
    Error,
}

impl OperationOutcome {
    #[inline]
    fn as_str(self) -> &'static str {
        match self {
            Self::Success => "success",
            Self::Error => "error",
        }
    }
}

impl ScribeMetrics {
    pub(crate) fn new(loaded_models: usize) -> Result<Arc<Self>> {
        let telemetry = TelemetryConfig::Library {
            name: "galadril.scribe",
            version: env!("CARGO_PKG_VERSION"),
        }
        .configure()?;
        let meter = telemetry.meter();
        let loaded_models = u64::try_from(loaded_models).unwrap_or(u64::MAX);
        let metrics = Arc::new(Self {
            _telemetry: telemetry,
            generations: meter
                .u64_counter("gen_ai.client.operation.count")
                .with_description("number of terminal Scribe messages")
                .build(),
            generation_duration: meter
                .f64_histogram("gen_ai.client.operation.duration")
                .with_description("Scribe generation duration")
                .with_unit("s")
                .build(),
            generated_token_counter: meter
                .u64_counter("gen_ai.client.token.usage")
                .with_description("number of generated model output tokens")
                .with_unit("{token}")
                .build(),
            active_session_gauge: meter
                .u64_gauge("gen_ai.client.session.active")
                .with_description("number of active Scribe sessions")
                .with_unit("{session}")
                .build(),
            active_generation_gauge: meter
                .u64_gauge("gen_ai.client.generation.active")
                .with_description("number of active Scribe generations")
                .with_unit("{generation}")
                .build(),
            loaded_model_gauge: meter
                .u64_gauge("gen_ai.client.model.loaded")
                .with_description("number of loaded Scribe models")
                .with_unit("{model}")
                .build(),
            tool_call_counter: meter
                .u64_counter("gen_ai.tool.call.count")
                .with_description("number of terminal Scribe tool calls")
                .build(),
            tool_duration: meter
                .f64_histogram("gen_ai.tool.call.duration")
                .with_description("Scribe tool call duration")
                .with_unit("s")
                .build(),
            completion_delivery_failure_counter: meter
                .u64_counter("gen_ai.client.completion.delivery.failure")
                .with_description(
                    "number of undeliverable terminal persistence notifications",
                )
                .build(),
            generated_tokens: AtomicU64::new(0),
            completed_messages: AtomicU64::new(0),
            failed_messages: AtomicU64::new(0),
            active_sessions: AtomicU64::new(0),
            active_generations: AtomicU64::new(0),
            loaded_models,
            tool_calls: AtomicU64::new(0),
            failed_tool_calls: AtomicU64::new(0),
            completion_delivery_failures: AtomicU64::new(0),
        });
        metrics.loaded_model_gauge.record(loaded_models, &[]);
        Ok(metrics)
    }

    #[inline]
    pub(crate) fn snapshot(&self) -> ScribeMetricsSnapshot {
        ScribeMetricsSnapshot {
            generated_tokens: self.generated_tokens.load(Ordering::Relaxed),
            completed_messages: self
                .completed_messages
                .load(Ordering::Relaxed),
            failed_messages: self.failed_messages.load(Ordering::Relaxed),
            active_sessions: self.active_sessions.load(Ordering::Relaxed),
            active_generations: self
                .active_generations
                .load(Ordering::Relaxed),
            loaded_models: self.loaded_models,
            tool_calls: self.tool_calls.load(Ordering::Relaxed),
            failed_tool_calls: self.failed_tool_calls.load(Ordering::Relaxed),
            completion_delivery_failures: self
                .completion_delivery_failures
                .load(Ordering::Relaxed),
        }
    }

    #[inline]
    pub(crate) fn session_started(&self) {
        let active = self.active_sessions.fetch_add(1, Ordering::Relaxed) + 1;
        self.active_session_gauge.record(active, &[]);
    }

    #[inline]
    pub(crate) fn session_ended(&self) {
        Self::decrement_gauge(
            &self.active_sessions,
            &self.active_session_gauge,
            "scribe.metrics.active_sessions.underflow",
        );
    }

    #[inline]
    pub(crate) fn generation_started(self: &Arc<Self>) -> GenerationGuard {
        let active =
            self.active_generations.fetch_add(1, Ordering::Relaxed) + 1;
        self.active_generation_gauge.record(active, &[]);
        GenerationGuard {
            metrics: Arc::clone(self),
        }
    }

    #[inline]
    pub(crate) fn record_generation(
        &self,
        started_at: Instant,
        model_alias: &str,
        outcome: OperationOutcome,
        generated_tokens: u64,
    ) {
        let attributes = [
            KeyValue::new("gen_ai.operation.name", "chat"),
            KeyValue::new("gen_ai.request.model", model_alias.to_owned()),
            KeyValue::new("gen_ai.operation.status", outcome.as_str()),
        ];
        self.generations.add(1, &attributes);
        self.generation_duration
            .record(started_at.elapsed().as_secs_f64(), &attributes);
        if generated_tokens > 0 {
            self.generated_token_counter
                .add(generated_tokens, &attributes);
            self.generated_tokens
                .fetch_add(generated_tokens, Ordering::Relaxed);
        }
        match outcome {
            OperationOutcome::Success => {
                self.completed_messages.fetch_add(1, Ordering::Relaxed);
            },
            OperationOutcome::Error => {
                self.failed_messages.fetch_add(1, Ordering::Relaxed);
            },
        }
    }

    #[inline]
    pub(crate) fn record_tool_call(
        &self,
        started_at: Instant,
        tool_name: &'static str,
        outcome: OperationOutcome,
    ) {
        let attributes = [
            KeyValue::new("gen_ai.tool.name", tool_name),
            KeyValue::new("gen_ai.tool.call.status", outcome.as_str()),
        ];
        self.tool_call_counter.add(1, &attributes);
        self.tool_duration
            .record(started_at.elapsed().as_secs_f64(), &attributes);
        self.tool_calls.fetch_add(1, Ordering::Relaxed);
        if outcome == OperationOutcome::Error {
            self.failed_tool_calls.fetch_add(1, Ordering::Relaxed);
        }
    }

    #[inline]
    pub(crate) fn record_completion_delivery_failure(&self) {
        self.completion_delivery_failure_counter.add(1, &[]);
        self.completion_delivery_failures
            .fetch_add(1, Ordering::Relaxed);
    }

    fn decrement_gauge(
        value: &AtomicU64,
        gauge: &Gauge<u64>,
        underflow_event: &'static str,
    ) {
        if value.load(Ordering::Relaxed) == 0 {
            tracing::error!(
                event.name = underflow_event,
                "Scribe metric state would underflow"
            );
            return;
        }
        let active = value.fetch_sub(1, Ordering::Relaxed) - 1;
        gauge.record(active, &[]);
    }
}

pub(crate) struct GenerationGuard {
    metrics: Arc<ScribeMetrics>,
}

impl Drop for GenerationGuard {
    fn drop(&mut self) {
        ScribeMetrics::decrement_gauge(
            &self.metrics.active_generations,
            &self.metrics.active_generation_gauge,
            "scribe.metrics.active_generations.underflow",
        );
    }
}

#[cfg(test)]
#[cfg_attr(coverage, coverage(off))]
mod tests {
    use super::*;

    #[test]
    fn operation_outcome_labels_are_stable() {
        assert_eq!(OperationOutcome::Success.as_str(), "success");
        assert_eq!(OperationOutcome::Error.as_str(), "error");
    }

    #[test]
    fn snapshot_exposes_complete_runtime_state() -> Result<()> {
        let metrics = ScribeMetrics::new(2)?;
        metrics.session_started();
        {
            let _generation = metrics.generation_started();
            metrics.record_generation(
                Instant::now(),
                "writer",
                OperationOutcome::Success,
                7,
            );
            metrics.record_tool_call(
                Instant::now(),
                "query_database",
                OperationOutcome::Error,
            );
        }
        metrics.record_completion_delivery_failure();

        assert_eq!(
            metrics.snapshot(),
            ScribeMetricsSnapshot {
                generated_tokens: 7,
                completed_messages: 1,
                failed_messages: 0,
                active_sessions: 1,
                active_generations: 0,
                loaded_models: 2,
                tool_calls: 1,
                failed_tool_calls: 1,
                completion_delivery_failures: 1,
            }
        );

        metrics.session_ended();
        assert_eq!(metrics.snapshot().active_sessions, 0);
        Ok(())
    }

    #[test]
    fn failed_generation_updates_failure_count_without_tokens() -> Result<()> {
        let metrics = ScribeMetrics::new(1)?;

        metrics.record_generation(
            Instant::now(),
            "writer",
            OperationOutcome::Error,
            0,
        );

        let snapshot = metrics.snapshot();
        assert_eq!(snapshot.completed_messages, 0);
        assert_eq!(snapshot.failed_messages, 1);
        assert_eq!(snapshot.generated_tokens, 0);
        Ok(())
    }

    #[test]
    fn ending_inactive_session_does_not_underflow() -> Result<()> {
        let metrics = ScribeMetrics::new(1)?;

        metrics.session_ended();

        assert_eq!(metrics.snapshot().active_sessions, 0);
        Ok(())
    }
}
