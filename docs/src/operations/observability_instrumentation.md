# Performance and incident diagnostics

## Collection profiles

| Profile | Signals | Where to run |
| --- | --- | --- |
| CI | The existing suite starts only its declared services. Pipeline integration uses Collector and Tempo where required. Bazel publishes phase durations and bounded incident summaries as undeclared test outputs. | BuildBuddy Linux |
| Benchmark | Full-traffic histograms, 5% of ordinary traces, and CPU and low-frequency off-CPU sampling on the selected services. Use PostgreSQL snapshots around a fixed workload. | Isolated Linux |
| Diagnostic | A five-second initial trace decision window and 100% success sampling for short reproductions where the request waterfall needs to appear quickly. Alloy profiles the selected targets every 30 seconds. | Isolated Linux with a narrow workload and a defined end time |

The default Compose stack retains 2% of ordinary traces and skips Alloy and
Pyroscope startup. Use the same Compose project and environment files as the
existing deployment. Append the selected preset after the normal environment
file and start that profile:

```sh
docker compose --env-file .env \
  --env-file infrastructure/observability/benchmark.env \
  -f infrastructure/docker/docker-compose.yaml \
  --profile observability-benchmark up -d otel-collector profiler
```

For short diagnostic runs, use `diagnostic.env` and the
`observability-diagnostic` profile. Stop the profiler and Pyroscope after the
window. Recreate the Collector with the normal environment to restore the
ordinary sampling percentage. The dedicated profiles share the CPU and RAM
limits in Compose. Adjust the preset's target regex to the containers needed
for the experiment.

## Dashboards

| Dashboard | Evidence and navigation |
| --- | --- |
| Galadril Overview | Gateway HTTP throughput, failures and available latency histograms, Vision throughput and resource pressure. |
| Pipeline Performance | Recent Tempo traces and a service-by-service waterfall when opening a trace. Vision histograms report p50, p95 and p99 for serialization, Ray round trip, actor execution and ledger persistence. Ray round trip includes queueing, transport and execution. Compare wall-clock phases on the same trace for critical-path analysis. |
| Authorization & GOL | SpiceDB permission RPC volume and latency, with structured Gateway denials and errors. |
| Database Performance | Open sessions, transaction outcomes, deadlocks and block reads. Query IDs and blocker detail are available separately in the restricted SQL workflow. |
| Kafka & Ray | Consumer group lag from broker and committed offsets, Kafka request bytes, in-flight Ray dispatches, task outcomes, and native per-node CPU, memory and disk metrics. |
| Error Analysis | Loki records grouped by canonical code and phase. Use the occurrence ID to find observations of the same failure and the trace link to open available spans. |
| Observability Health | Scrape availability, active series observed on scrapes, collector memory, export failures, sampling decisions, late spans and profiler drop counts. |
| CI Performance | Link to the real GitHub CI runs and their BuildBuddy invocations. BuildBuddy provides timing, cache and critical-path views. There is no continuous Grafana ingestion of BuildBuddy measurements. |

Grafana refreshes recent pipeline traces every ten seconds. A new trace first
needs to pass the Collector's decision window and reach Tempo. Opening a trace
in Grafana shows span start and finish times in a waterfall. Loki links use
`trace_id`. Mimir exemplars also use this key when an exported trace is
available. Ray counts are based on healthy scraped agents, so a failed scrape
is visible in Observability Health. Compose scrapes the embedded Ray node over
the internal network. For a shared Ray cluster, configure discovery of every
node's metrics agent and apply the same metric allowlist.
[Ray Prometheus discovery](https://docs.ray.io/en/latest/cluster/metrics.html)

Find CPU and off-CPU flame graphs in Pipeline Performance's **Continuous
profiling** section. Tempo links to the service's profile over the trace
interval. The service and time range correlate with the trace. An asynchronous
span can include time on a different node, so use the execution span and node
metrics to interpret the graph.

Alloy eBPF needs a compatible Linux kernel and access to the workload's PID
namespace. On macOS, the relevant kernel is the container VM, and its
capabilities vary by runtime. Check `up{job="profiler"}`, the target and drop
counters in Observability Health, and the resulting CPU profile series in
Pyroscope before treating any flame graph as evidence. Run the profiling
experiment on Linux when the VM cannot attach the profiler. Pyroscope volume
growth alone does not demonstrate that any new profile was received.
[Alloy eBPF requirements](https://grafana.com/docs/alloy/latest/reference/components/pyroscope/pyroscope.ebpf/)

## Sampling and loss

Compose routes OTLP through a single Collector. Every span with the same trace
ID reaches that Collector while it is available. If multiple tail samplers are
deployed, route by trace ID before sampling. The metrics and operational log
pipelines run independently from the trace sampler.

The canonical policy prioritizes IFC violations, conflicts, unavailability,
timeouts, invalid results and internal failures. Unclassified error status and
traces lasting over five seconds are also retained. Recovered attempts that
emit a prioritized code can still retain their trace. Outcome and retry
attributes in structured records show what happened. Protected audit context
is removed from operational exports.

The initial values are a 90-second decision window, 4,000 pending traces,
40 expected new traces per second and a 64 KiB limit per trace. These are
starting values for the Compose deployment. Size them with observed new-trace
rate, span arrival lag and the memory limit. Sampled and unsampled decision
caches handle late spans. A late span cannot revise a previous decision while
that decision remains cached. An oversized trace, refusal, eviction, failed
export, Collector restart or expired cache can still lose evidence. For an
incident, start with the canonical failure event or durable pipeline record
when a trace is missing.
[Collector tail-sampling behavior and internal metrics](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.162.0/processor/tailsamplingprocessor)

## PostgreSQL contention

The database image preloads `pg_stat_statements`. Run the following in a
restricted diagnostic session against a representative isolated deployment.
The first query aggregates lock modes, wait events, blocker presence,
transaction age, deadlocks and PostgreSQL 18 I/O counters. The second ranks
statements by cumulative latency, frequency, mean latency and block I/O:

```sh
psql --no-psqlrc --set=ON_ERROR_STOP=1 --file=database/diagnostics/snapshot.sql
psql --no-psqlrc --set=ON_ERROR_STOP=1 --file=database/diagnostics/statements.sql
```

Take snapshots before and after the same cohort. A high calls delta is a
candidate for an N+1 pattern when multiplied by the number of records in a
request. Validate it against the request span or controlled changes in batch
size. Check repeated scans against the plan's actual loop and row counts.
Set `track_io_timing` in the isolated benchmark when time spent in I/O needs
to be measured.

Run `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` on selected read-only queries
as the same non-owner role with the same transaction-local tenant context as
the application. Verify RLS with a second tenant in the fixture. The optional
`database/diagnostics/auto-explain.sql` enables sampled plan logging only in
the psql session where it is loaded. Load it in an administrator session in the
isolated database, then execute the fixture workload with its application role
and RLS active. The plans may contain schema details or literals. Keep them on
the restricted diagnostic host and outside Grafana and BuildBuddy.
[PostgreSQL 18 wait and I/O statistics](https://www.postgresql.org/docs/18/monitoring-stats.html)
[PostgreSQL 18 auto_explain](https://www.postgresql.org/docs/18/auto-explain.html)

## Cost and attribution

Current Compose limits are configurable with each service's
`GALADRIL_<SERVICE>_CPUS` and `GALADRIL_<SERVICE>_MEMORY` settings. Defaults
range from 0.25 to 1 CPU and 128 MiB to 1 GiB per observability container.
Docker keeps two 10 MiB log files by default per observability container.
Loki retains logs for 72 hours with a 2 MB/s sustained ingestion limit and
5,000 active streams. Tempo retains trace blocks for 24 hours. Mimir retains
blocks for seven days with a cap of 50,000 series and 5,000 exemplars.
Opt-in Pyroscope retains profiles for 24 hours. Time retention allows
compactor lag and does not impose a byte quota on the Docker volume.

For each profile, use a fresh isolated Compose project and record its image
digests, workload, duration, node count and start/stop timestamps. Record
`docker stats --no-stream` at fixed intervals for CPU and RSS estimates and
at both endpoints for network I/O and block I/O deltas. Record the verbose
`docker system df` volume table before and after. Compare Tempo WAL and
blocks, Loki chunks and index, Mimir head and blocks, Pyroscope profile
blocks, Alloy symbol cache, Grafana data and rotated container logs. Report
network bytes per completed request and storage growth per hour as well as
absolute size. Change one parameter at a time. Include the Collector's lost
span and exporter errors so lost evidence is visible alongside byte savings.

A stopped local Docker snapshot included 513 MB for Grafana, 79 MB for
Pyroscope, 23 MB for Mimir and 18 MB for Tempo. Alloy's state and symbol-cache
volumes together used 112 B. Docker reported 10.65 GB across all volumes.
Current-day activity was about ten minutes and the volumes already existed.
The snapshot provides no growth rate, per-component CPU/RAM/network baseline
or proof of successful Alloy collection. The database image build later
encountered an OrbStack outage with roughly 170 MiB free on the Mac.
A matched before/after run is still needed for cost claims.

## Diagnostic access

The Compose backend ports bind to loopback. Keep the stack and its volumes in
the operator trust boundary. These local single-tenant backends are unsuitable
as a shared tenant-facing observability service without an authenticated proxy
and access policies. Use the existing protected authorization audit store for
principals, permissions and IFC labels. The operational path uses stable codes,
phases, attempt numbers and occurrence IDs. Keep raw SQL plans, credentials,
customer payloads and profiles of unrelated processes outside CI artifacts.

The Bazel `integration-performance.json` report records startup and scenario
durations before teardown. For failures, it also attempts to include catalog
events, wait aggregates and top statement fingerprints. A missing section means
that snapshot was unavailable. The BuildBuddy invocation provides the Bazel
critical path and cache information.
