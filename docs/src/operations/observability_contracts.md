# Observability contracts

The [Weaver registry][registry] defines Galadril's business telemetry and
references the official OpenTelemetry conventions for HTTP, RPC, databases,
messaging, CI and generative AI. Bazel pins Weaver 0.28.0 and the OpenTelemetry
registry 1.41.0 by archive digest. Weaver resolves the registry offline from
Bazel inputs. The existing SDKs and OTLP exporter lifecycle handle delivery.

## Naming and classification

Use `galadril.<domain>.<operation>[.<phase>]` for fixed business span and
event names. Use `service.name` for the process identity and the native OTLP
event name, timestamp, trace ID and span ID for logs. Keep identifiers out of
signal names. Put allowed operation and outcome enums on metrics with
finite, reviewed value sets. Put per-message UUIDs, attempt numbers and
immutable revision coordinates on explicitly declared traces or logs. User
input, tenant names and custom step names have unbounded cardinality.

Operational failure events carry the application code and occurrence UUID.
Vision additionally records the phase at which each component observed the
failure, the Kafka command UUID and the attempt index. The Kafka result retains
the original phase. Startup or control-plane logs can use a separate event
when there is no command. For one asynchronous command use its W3C parent.
Use span links for independent parents when combining multiple messages.
Vision's existing batch UDF instrumentation accepts trace parents as links.

Keep verified principal IDs, workload IDs, permissions and IFC labels in
protected audit storage. The production Collector drops audit events from
the operational pipeline and strips the reserved audit attribute namespace.
The existing Gateway audit read path requires tenant administrator authority
and the store scopes the query to the tenant. Keep policies, permits, receipts,
tokens, exception messages and business content out of exported diagnostics.
Trace IDs, message IDs and failure UUIDs provide correlation. Authority and
provenance come from the original authenticated service context and verified
artifacts.

## Error transport and retry

[The catalog][errors] defines meaning, category, confidentiality, conditional
retry and HTTP/gRPC mappings. The GraphQL canonical extension uses the code
as its value. Public API adapters that still use legacy codes retain their
existing behavior while they are migrated. Ray and Kafka carry `failure.code`,
`failure.occurrence_id` and `failure.phase`. The private Gateway worker HTTP
adapter returns the same code and UUID in `X-Galadril-Error-Code` and
`X-Galadril-Error-Occurrence-Id`. Vision uses the response status to classify
transport failures from peers without those headers.

Assign a random occurrence UUID at the origin and preserve it when wrapping
or forwarding. Keep the exception chain in the originating process. When
publishing, serialize the typed failure fields only. An attempt can retry
only when the catalog and the operation both permit it. The Vision command
handler retries transient failures within the idempotent command's configured
attempt budget. A change to authority or concurrent state requires fresh
admission or state acquisition. A new execution failure has its own occurrence.

| Outcome | Durable state and action |
| --- | --- |
| Access denied after dispatch | Store the failure and publish the authorized dead-letter record. |
| Transient failure with attempts remaining | Store the failure and confirm the next Kafka command. |
| Running lineage refused before dispatch | Release the running claim for broker redelivery under a fresh check. |
| Cached completion | Validate scope and republish permitted successors without executing the step again. |
| Dispatch cancelled | Decrement the active metric and preserve the existing cancellation path. |

## Contributing

1. Reuse an official attribute or signal where available. Define the fixed
   name, kind, requirement levels and stability in the registry. Describe units
   and finite dimensions for metrics. Classify each Galadril attribute.
2. Add an error to `registry/errors.yaml` when recovery or handling needs a
   distinct meaning. Keep published meanings and codes stable. Define the
   transport mapping and the conditions required for retry. Put safe text in
   `brief` and keep runtime details in the local exception chain.
3. Build the generated bindings and refresh the source copies used by Cargo,
   the Python distribution and Nuxt. Commit the registry and bindings together.

   ```sh
   bazel build //platform/telemetry/contracts:generate
   cp bazel-bin/platform/telemetry/contracts/generated/contracts.rs platform/telemetry/src/generated/contract.rs
   cp bazel-bin/platform/telemetry/contracts/generated/contracts.py platform/pipeline/src/galadril_pipeline/telemetry_contract.py
   cp bazel-bin/platform/telemetry/contracts/generated/contracts.ts front/dashboard/server/auth/telemetry-contract.ts
   bazel test //platform/telemetry/contracts/...
   ```

4. Use the generated Rust event emitters, Python attribute builders or
   TypeScript metric contracts with the existing SDK. Add a test of the
   emitted OTLP signal and update affected Collector, Grafana or E2E consumers.
   For a change to an already published version, run `weaver registry diff`
   against the previous Galadril model. The checked-in test compares the initial
   Galadril model against the pinned upstream baseline.
5. For a plugin registry, use a unique schema URL under
   `https://galadril.aeluin.com/schemas/plugins/<plugin>/<version>`.
   Declare the core model dependency and use
   `galadril.plugin.<plugin>.<operation>[.<phase>]`. Run Weaver check with
   `platform/telemetry/contracts/plugin_namespace.rego`. The Bazel plugin
   test stages and validates one such registry and rejects a conflicting name.

## Audit and rollout evidence

| Boundary | Existing behavior and migration status |
| --- | --- |
| Rust SDK, Gateway and Intake | `platform/telemetry/src/lib.rs` owns the OTLP bridge and W3C propagation. Gateway authorization checks formerly included principal and resource identifiers and a duplicate service field. The migrated event carries decision and optional structured failure. HTTP Intake metrics and other Rust error calls still need a convention-by-convention pass. |
| Registry and GOL | Registry uses hand-authored tracing around compilation and publication in `platform/registry/src/gol_service.rs` and `composition.rs`. Private GOL HTTP responses previously dropped occurrence identity. The private adapter now returns an explicit code and UUID. Public GraphQL administration and pipeline adapters still classify some legacy errors by message. |
| Vision, Kafka and Ray | FastStream extracts context and dispatch injects it into Ray. Actor status and logs previously attached raw exception text and dispatcher errors lost the identity. Actor, dispatcher and dead letter now carry structured failures. Workflow-configured names were removed from the migrated metric labels. |
| IFC, LI-ESKG, Amarth and Scribe | The registry defines initial operation names. The database, causal and agent instrumentation has independent names today. Scribe uses both business spans and provider-specific metrics in `machine-learning/scribe/python/galadril_scribe`. Exact IFC classification and the wider adapter migration remain separate work. |
| Plugins | `plugins/src/galadril_plugins/telemetry.py` has its own SDK setup and JSON log renderer. The registry extension works without referring to plugin capabilities in core. Existing plugin workers keep their current exporters. |
| TypeScript Studio | The server intentionally exports opt-in aggregate metrics. Generated operation/outcome values and the Collector allowlist remain bounded. Browser traces and identities stay outside that export. |
| Audit and correlation | Existing authorization audit storage is separate from OTLP. Full IFC label and GOLC version capture is not wired across all services. The registry defines fields for subsequent audited integrations and the ordinary Collector excludes protected fields. |

The focused Bazel gate runs Weaver check, generate, diff and live-check. It
compares independently generated source bytes, checks the released error
catalog and plugin namespace, and feeds real OTLP gRPC from the Vision
adapters to Weaver. The Docker lifecycle E2E additionally checks the real
Kafka, Gateway and Ray trace, matches a Ray failure UUID to a dead letter, and verifies positive and negative Gateway audit
access. The isolated adapter tests exercise failure correlation and privacy.
Use the normal BuildBuddy-backed Bazel presubmit for the container lifecycle.

[registry]: ../../../platform/telemetry/contracts/registry/
[errors]: ../../../platform/telemetry/contracts/registry/errors.yaml
