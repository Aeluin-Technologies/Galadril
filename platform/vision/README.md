# Galadril Vision

Galadril Vision is a real-time, multi-tenant pipeline for multimodal inference,
entity resolution, graph persistence, and causal analysis.

## Architecture

FastStream owns Kafka/Redpanda consumption, Pydantic v2 validation, routing,
acknowledgements, retry publication, and W3C trace extraction. Long-running,
CPU-heavy, and GPU-heavy operations execute in named Ray actors. Kafka delivery
is at least once; the Postgres execution ledger and deterministic command IDs
provide logical idempotency.

```mermaid
flowchart LR
    R["Rust intake"] -->|"Confluent Avro + traceparent"| K[("Kafka / Redpanda")]
    K --> I["FastStream ingress"]
    I -->|"Pydantic v2"| C[("CPU commands")]
    I --> G[("GPU commands")]
    I --> A[("Causal commands")]
    C --> CW["FastStream CPU worker"]
    G --> GW["FastStream GPU worker"]
    A --> AW["FastStream causal worker"]
    CW -->|"W3C carrier"| RA["Ray CPU actor"]
    GW -->|"W3C carrier"| RG["Ray GPU actor"]
    AW -->|"W3C carrier"| RC["Ray causal actor"]
    RA --> P[("Postgres / AGE / pgvector")]
    RG --> P
    RC --> P
    RA --> K
    RG --> K
    RC --> K
    I -. "OTLP" .-> O["OpenTelemetry Collector"]
    CW -. "OTLP" .-> O
    GW -. "OTLP" .-> O
    AW -. "OTLP" .-> O
    RA -. "OTLP" .-> O
    RG -. "OTLP" .-> O
    RC -. "OTLP" .-> O
```

The services are split by role so Kafka partitions and replicas provide bounded
consumer concurrency while Ray actor replicas provide resource isolation. A
message is acknowledged only after confirmed downstream/DLQ publication.

## Observability

- Incoming W3C `traceparent` headers are extracted automatically by FastStream.
- The current context is serialized explicitly for Ray and extracted before the
  actor span starts, preserving the Trace ID across the process boundary.
- `/metrics` exposes FastStream throughput/size/latency plus pipeline latency,
  throughput, and active Ray task instruments.
- OTLP traces, metrics, and logs flow through the collector to Jaeger,
  Prometheus, and Loki. Grafana provisions all three data sources.
- JSON logs include `trace_id`, `span_id`, `entity_id`, `pipeline`, and `step`.

## Running locally

```bash
docker compose -f infrastructure/docker/docker-compose.yaml up
```

Enable the declared GPU Ray worker where NVIDIA container support is available:

```bash
docker compose -f infrastructure/docker/docker-compose.yaml --profile gpu up
```

Service endpoints are `:8000` through `:8003`, Ray Dashboard is `:8265`,
Jaeger is `:16686`, Prometheus is `:9090`, and Grafana is `:3005`.
