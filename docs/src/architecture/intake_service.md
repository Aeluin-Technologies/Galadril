# Intake service

Intake service is a Rust binary located in `services/intake/`. Its sole
responsibility is ingestion.

## Initialization Phase

At startup, Intake reads trusted settings from `connectors.yaml`, loads local
Avro schemas, and connects to Kafka, S3, and the TerminusDB pipeline catalog.
Pipeline routing definitions come only from published, non-deleted tenant
revisions in TerminusDB. Compiled routes expire after five seconds.
See [pipeline storage](../configuration/pipeline_storage.md).

## The Event Loop

The service continuously listens to the S3 bucket notification topic. When a
file arrives:

1. **Authorization**: It extracts the exact tenant partition from the object key
   and verifies that tenant exists in the trusted TerminusDB capability map.
2. **Routing**: It compares the tenant-scoped path with every published source
   rule. A shared source can produce one route for each matching immutable
   pipeline publication.
3. **Parsing**: It downloads the tenant object once and selects the configured
   parser for each route.
4. **Emitting**: It publishes the parsed record with mandatory
   `galadril-tenant-id`, `galadril-pipeline-id`, and
   `galadril-pipeline-revision` Kafka headers. Vision rejects missing,
   malformed, or conflicting identity metadata.

S3 keys are partitioned as `<tenant>/...` and tenant comparison is exact and
case-sensitive. Intake does not create PostgreSQL temporary state. Components
that use PostgreSQL set the tenant transaction context before accessing tables
protected by row-level security.
