---
name: galadril-distributed-pipelines
description: >-
  Use for Galadril Kafka/Redpanda ingestion, FastStream acknowledgement,
  Ray execution, pipeline routing, asynchronous retries, graph persistence
  or distributed worker state. Not for a purely local transformation.
---
# Distributed pipeline correctness

Read `platform/pipeline/README.md`, `platform/vision/README.md` and the
applicable `docs/src/architecture/pipeline_routing.md` before changes.

1. Separate responsibilities: durable workflow log in Kafka/Redpanda,
   delivery and acknowledgement in FastStream, compute placement in Ray,
   immutable pipeline and ontology authority in Registry.
2. Verify admission from trusted tenant, pipeline and revision context.
   Reject conflicting headers/payloads and unpublished or unauthorized
   routes; do not let a model choose trusted handler factories or credentials.
3. Specify delivery semantics. Plan for duplicates, partial completion,
   timeouts, crash/restart and replay; do not claim an exactly-once guarantee
   across services without proof.
4. Use tenant- and revision-qualified idempotency/deduplication keys where
   appropriate. Preserve atomic domain writes and authorization-outbox
   creation when that is the existing ownership boundary.
5. Protect ordering, watermarks and late data explicitly. Non-scheduled
   steps have one upstream input in the present pipeline contract; a
   multi-input join needs an intentional stateful processor.
6. Inspect Ray actor lifecycle, shared model/pool ownership, bounded
   concurrency, backpressure, cleanup and context reset after failures.
   Optimize allocations only with measurements; avoid cross-tenant shared
   mutable state.
7. Run deterministic Bazel cases for duplicate delivery, retry,
   interruption, stale revision, wrong tenant, route transfer and partial
   outbox delivery. Report checks impossible to perform in the environment.

Deliver a short failure-mode matrix: scenario, owner, durable state,
recovery behavior, corresponding test. Consult
`../../references/GALADRIL_CONTRACTS.md` for source locations.
