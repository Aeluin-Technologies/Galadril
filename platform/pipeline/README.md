# Galadril Pipeline Contracts

This package contains the immutable Pydantic v2 event contracts, validated
pipeline configuration, and startup-compiled routing table shared by
FastStream gateways and Ray actors. It intentionally contains no scheduler or
in-process execution engine: Kafka/Redpanda is the durable workflow log,
FastStream owns delivery and acknowledgement, and Ray owns compute placement.

Non-scheduled steps must have exactly one upstream input. Multi-input joins
require an explicit stateful stream processor so replay, watermarks, and late
data semantics are designed rather than inferred from a batch DAG.
