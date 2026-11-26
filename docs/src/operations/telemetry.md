# Galadril telemetry contract

Galadril binaries use OpenTelemetry SDKs and send logs, metrics, and traces to
the intermediary collector with OTLP. Application
containers do not expose scrape endpoints and do not select observability
backends. Exporters use bounded queues and background workers; queue overflow
drops telemetry instead of delaying application work.

## Resource attributes

Every signal carries these lowercase resource attributes:

| Attribute | Example |
| --- | --- |
| `service.name` | `galadril-intake` |
| `service.version` | `0.1.1` |
| `deployment.environment` | `production` |
| `service.instance.id` | container hostname |
| `telemetry.sdk.language` | `rust` or `python` |

`service.name` is stable across worker roles so traces, metrics, logs, and
profiles join on the same value. Instance and role data belong in separate
attributes rather than in the service name.

## Signal contracts

The [observability contracts](observability_contracts.md) define the
shared rules for structured events, metrics, errors and W3C propagation.
Studio's opt-in aggregate metrics exporter uses OTLP over HTTP.

## Continuous profiling

Grafana Alloy profiles the Rust and Python containers out of process with the
OpenTelemetry eBPF profiler. It samples at 19 Hz and ships a pprof-compatible
batch every 30 seconds to the central Pyroscope service. Python process memory,
virtual memory, garbage collection, CPU, and thread measurements are collected
as asynchronous OpenTelemetry metrics, allowing profile windows to be aligned
with memory or CPU changes without adding work to the application event loop.

The continuous pprof stream is a CPU profile. The eBPF profiler does not support
allocation profiles; keeping malloc or Python allocator uprobes permanently
enabled would also violate the production overhead constraint. Use the memory
and garbage-collection metrics to select a narrow investigation window, then
run an explicitly enabled allocation-profile session for that window. Do not
describe process-memory metrics as allocation stack profiles.

The eBPF profiler requires a Linux host, the host PID namespace, and the kernel
capabilities configured in `infrastructure/docker/observability.yaml`. Docker
Desktop hosts that do not expose Linux eBPF facilities will run the remaining
telemetry pipeline but cannot produce profiles.

## Environment

Applications use the standard `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, `OTEL_TRACES_SAMPLER`, and
`OTEL_TRACES_SAMPLER_ARG` variables. `DEPLOYMENT_ENVIRONMENT` supplies the
environment resource value for Rust; validated pipeline telemetry configuration
supplies it for Python.
