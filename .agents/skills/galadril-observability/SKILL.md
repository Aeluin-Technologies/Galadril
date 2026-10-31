---
name: galadril-observability
description: >-
  Use when adding or reviewing Galadril OTLP logs, metrics, traces or alerts,
  especially across Kafka/FastStream/Ray/service boundaries, or when an
  incident lacks diagnostic evidence. Not a profiler or live incident debugger.
---
# Observability with diagnostic purpose

Existing conventions take priority: read `AGENTS.md`, `platform/vision/README.md`
and the relevant telemetry implementation. Galadril already exports OTLP to
a collector; do not introduce a parallel observability stack.

1. State 2-4 concrete questions operations need to answer (e.g., which
   pipeline revision is failing? is queue age increasing? which actor hop
   dominates p99?). Add only signals that answer them.
2. Choose structured logs for discrete events and causes; bounded-cardinality
   metrics for rates/errors/durations and queue/resource saturation; traces
   for cross-boundary causality. Use histograms for latency percentiles.
3. Preserve W3C trace context across intake -> Kafka/Redpanda -> FastStream
   -> Ray and other service boundaries; verify propagation from actual
   instrumented paths. Trace headers and OTel baggage are not identity or
   authorization evidence.
4. Use stable event names, service/step identifiers and bounded outcome
   dimensions. Do not use tenant IDs, entity IDs, user IDs, raw URLs, error
   text or arbitrary model outputs as metric labels. If tenant identity is
   needed in a restricted audit event, apply privacy/access policies.
5. Never log tokens, prompts, payload bodies, raw documents, embeddings or
   sensitive inferred identities. Review data minimization and retention.
6. Instrument retry, timeout, deduplication, dead-letter and queue-age paths
   where operationally meaningful. Do not add spans to every render or loop.
7. Check real emitted data, broken context links and expected failure signals.
   Set alert thresholds from SLOs or measured baselines, not invented values;
   provide a short runbook for actionable alerts.

Report each operational question, signal, label cardinality, validation and
remaining blind spots. Use `../../references/GALADRIL_CONTRACTS.md` where
ownership is unclear.
