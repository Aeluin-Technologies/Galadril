---
name: galadril-performance
description: >-
  Use when measuring or optimizing Galadril latency, throughput, memory,
  allocations, high-concurrency behavior, Ray resource usage or hot paths.
  Not for unmeasured routine refactoring or observability design.
---
# Evidence-led performance work

1. Define a representative workload, scope, target metric and known resource
   limits. Record hardware, software versions, input size, tenant mix and
   relevant concurrency; avoid arbitrary success thresholds.
2. Establish a baseline: throughput, p50/p95/p99, peak/steady RSS, allocation
   or GC pressure, relevant CPU/GPU utilization and queue latency. Use only
   measurements the environment can actually produce.
3. Attribute the bottleneck with traces/profilers and code inspection:
   I/O vs CPU, serialization, copies, lock contention, queueing, cache
   miss, context switch, GPU transfer or model startup. Distinguish wall
   time from service time.
4. Propose the smallest structural remedy. Respect Galadril's zero-copy,
   bounded memory and Rust error-handling rules, but verify that a proposed
   copy removal or cache really improves the workload.
5. For shared workers, measure tenant fairness, backpressure and noisy-neighbor
   effects. Never trade away tenant isolation, version pinning, correctness
   or safe cancellation for aggregate throughput.
6. Re-run the same benchmark and applicable Bazel correctness gates. Account
   for warmup, variance, test data, and nonrepresentative local hardware.

## Output

Show baseline versus candidate, reproducibility, uncertainty, correctness
checks, resource regressions and rollback criteria. If profiling is blocked,
provide an instrumented hypothesis, not a claimed speedup. For signal design
use `$galadril-observability`; do not confuse logging with profiling.
