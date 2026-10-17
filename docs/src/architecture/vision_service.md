# Vision Service

Vision Service is the brain of the platform, located in `platform/vision/`.
It is dynamically orchestrated by the `galadril-pipeline` library.
It relies on the `galadril-inference` library to standardize calls to ML
algorithms.

## Execution boundary

The application layer owns command admission, published step selection, and
ontology binding. It depends on pipeline and ontology contracts and a structural
`StepHandler` port, never on Ray, Kafka, S3, PostgreSQL, or inference engines.
Handlers receive an already admitted command and its published step definition;
they return a JSON object. They cannot select the route used for admission.

The actor composition root builds one route per execution identity and injects
handlers by published step ID. An execution identity pins a tenant, pipeline,
and revision. A command must match that identity, tenant, step ID, and step type
before any handler or ontology lookup runs. Duplicate routes, duplicate steps,
and missing or unknown handler bindings fail during construction. Route tables
and step definitions are snapshotted at construction.

The built-in adapter retains the current inference, resolution, transactional
sink, and causal implementations. Each route owns its adapter, S3 client,
and PostgreSQL state; configuration from one route must never initialize another
route's connections. Routes with identity resolution enabled for the same tenant
share one resolver owner and must agree on its configuration and database.
Incompatible identity bindings fail during composition. Pools and models are reused across commands
within their ownership boundary. The actor preserves context and accelerator
cleanup after success and failure. Existing Ray spans and structured OTLP logs
continue to wrap execution, including injected handlers.

Trusted Python composition can replace individual handlers, including resolution
and persistence, without changing command admission or the actor transport.
Handler bindings come exclusively from trusted Python composition. Pipeline
documents and command payloads cannot supply new handler factories or backend
credentials through this boundary. Existing configured model imports remain
unchanged. This is dependency injection, not a plugin loader or sandbox.
Handlers remain trusted service code and own their backend transaction, tenant
isolation, idempotency, and resource lifetime contracts.
The built-in sink keeps its PostgreSQL/AGE/vector/authz transaction together;
splitting it into independent graph and vector ports would lose that guarantee.

This first boundary does not make all Vision infrastructure interchangeable:
streaming startup, the execution ledger, and causal/query internals still use
PostgreSQL. Local examples retain their unscoped execution behavior. Production
routes come from validated Registry publications. There is currently no trusted
project identity in the runtime contract; project selection requires a future
Registry-owned binding, never a project ID supplied in command attributes.
Official plugin discovery, new step kinds, MCP, Skills, scheduling and approvals
remain subsequent work. No external plugin import is introduced here.

Trusted composition uses `VisionCommandProcessor(configs,
handler_factory=build_handlers)`. The factory receives one snapshotted
`VisionConfig` per route and returns a mapping from every published step ID to a
`StepHandler`. Multiple steps may share one route-owned adapter. An alternative
sink implements `async execute(command, step) -> dict[str, JsonValue]` without
PostgreSQL types. Custom factories are responsible for preserving a single
identity sequence owner when sharing a tenant across routes. Production deployment
still uses the built-in composition; exposing reviewed official implementations
in Registry is a later stage.

## DAG Construction

Service loads runtime settings from `connectors.yaml` and asks Registry for one
explicitly configured tenant and published pipeline. Registry validates the
tenant marker and returns the DAG pinned to an opaque immutable revision. Each
process keeps that tenant-specific route and ontology context while sharing its
Kafka consumers and Ray CPU, GPU, and causal actors. An explicitly selected
`pipeline.example.yaml` remains available for local examples.
See [pipeline storage](../configuration/pipeline_storage.md) for deployment and
revision activation.
If the tenant or publication does not exist yet, Vision waits with bounded
exponential backoff (1–30 seconds) and emits `pipeline.publication.pending` over
OTLP. Registry unavailability and deadlines also retry. No Kafka consumers or
Ray actors start until the requested publication is validated. Invalid scope,
invalid artifacts and authorization failures remain fatal. Provision the
configured tenant and publish its pipeline through Gateway; startup never
creates a tenant or substitutes a different DAG.
Registry validates cycles and dependencies before publication. Vision computes
only the execution order needed by its local runtime.

## Dynamic Model Loading
Models are not hardcoded. Service uses Python's `importlib` to instantiate the
exact classes defined in the configuration.
Model weights are automatically pulled from S3 into memory before processing
begins.

## Message Routing Loop

1. The service polls batches of messages from Kafka.
2. It validates the immutable tenant, pipeline, and revision Kafka headers and
   ensures the payload tenant agrees with the trusted header.
3. It selects exactly one published DAG by `(tenant, pipeline, revision)` and
   resolves the configured source within that DAG.
4. It passes the data to the model through a shared Ray resource pool.
5. After every command, including failures, the Ray actor clears request
   context variables, runs Python garbage collection, and clears the CUDA cache
   for GPU commands. LI-ESKG retains only its internally tenant-isolated runtime.
6. Once the model outputs a prediction, the service asks the selected DAG which
   step consumes the output next.
7. If no step needs it, the data has reached the end of the pipeline (a Sink).
