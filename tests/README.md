# End-to-end pipeline tests

## Purpose

The suite in `tests/e2e` validates the externally observable lifecycle of one
tenant-owned record:

```text
Gateway upload API -> S3 -> Intake -> Kafka -> Vision -> PostgreSQL/SpiceDB
                   -> Registry-pinned pipeline -> Gateway access API
```

The browser or tenant client never writes to the production bucket directly.
It requests an owner-scoped staging URL from Gateway, uploads to that URL, and
asks Gateway to promote the object. The test therefore exercises the real
Gateway-to-S3 path in addition to every asynchronous processing stage.

## Bazel and environment model

`//tests/e2e:pipeline_lifecycle_test` is the orchestration boundary. Bazel
builds OCI images from the current Gateway, Intake, Registry, and Vision
sources. The test loads those images into its isolated Docker daemon and uses
`tests/e2e/environment/compose.yaml` for infrastructure that cannot run as an
ordinary Bazel process: PostgreSQL, SpiceDB, Redpanda, MinIO, lakeFS, Tempo,
and the OTLP collector.

Run the suite through the workspace test graph:

```shell
bazel test //...
```

The target is tagged `integration`, `requires-docker`, and `no-sandbox`, and
uses the workspace Docker test execution properties. It is intentionally a
single lifecycle test so that expensive services start once and assertions
share one immutable upload identity.

## Scenario and assertions

The scenario performs these transitions in order:

1. Seed two active tenant identities and the minimum tenant relationships;
   verify the non-administrator cannot upload or publish.
2. Materialize and publish a small ontology, create a three-step pipeline
   through Gateway, bind every block to the ontology, and publish the exact
   Registry revision.
3. Request a staging upload through Gateway, upload a text fixture with the
   returned URL, and complete the upload through Gateway.
4. Wait for Intake to route the S3 notification and for Vision to finish the
   deterministic inference, resolution, and sink steps.
5. Read the derived entity through Gateway as the raw-data owner and verify
   that an unrelated tenant member receives no result.

The assertions span the service boundaries rather than re-testing internal
algorithms:

- Gateway rejects missing, malformed, and expired JWTs, GraphQL request bodies
  larger than the configured byte limit, and operations whose fragment-aware
  selection depth exceeds the configured recursion limit.
- Schema introspection must expose exactly the root fields with an explicit E2E
  disposition. The suite executes all 16 query fields and all 22 mutation
  fields; only the `ask` subscription is excluded because chat is not yet an
  available product surface.
- IAM user and role management, role assignment, inactive Cedar policy writes,
  and conversation/message create-read-update-delete flows run through the
  public Gateway API before the pipeline lifecycle.
- Gateway returns the canonical `<tenant>/raw/<group>/<object>` key and S3
  retains the trusted tenant, owner, issuer, permission, resource, and
  delegation metadata.
- Registry returns the exact published revision consumed by Vision.
- Intake accepts only Gateway-issued metadata and propagates the tenant,
  immutable pipeline identity, authentication provenance, and delegation ID.
- Vision records all pipeline steps, persists the derived entity, and drains
  its SpiceDB outbox.
- SpiceDB contains the raw ownership and derived `source` relationships;
  permission checks allow the uploader and deny an unrelated member.
- Gateway applies the same fine-grained decision to its structured search
  response, rejects a token for an unrelated tenant, and denies upload and
  publication mutations to a tenant member without the required permission.
- Kafka lineage events contain accepted, running, and completed transitions
  with one correlation ID and trace ID across the Vision DAG.
- Tempo contains that Intake-to-Vision trace and the caller-supplied Gateway
  trace.

## Fixtures and test-only components

`tests/e2e/fixtures/e2e_inference_model.py` is a deterministic, allocation-
bounded model mounted only into the Vision test container. It avoids network
model downloads and accelerator requirements while keeping the normal Vision
model loading, Ray dispatch, resolution, and sink code paths intact.

`tests/e2e/fixtures/connectors.yaml` disables LI-ESKG identity allocation for
this system test. Vision still performs vector candidate lookup and creates a
stable fallback entity through its production kill-switch path. This keeps the
test focused on pipeline integration rather than duplicating LI-ESKG's own
domain tests.

The ontology revision and block bindings are environment prerequisites. They
are written through Registry because Gateway currently exposes ontology
publication but not ontology revision authoring or block binding mutations.
Every available Gateway query and mutation, including pipeline creation,
publication, upload, and final data access, is exercised through GraphQL.
The unavailable `ask` subscription remains covered by its owning service tests
until chat can be included in the public lifecycle.

## Coverage boundaries

The E2E suite deliberately does not duplicate localized tests for filename
sanitization, parser variants, invalid DAGs, Avro schema compilation, retry
backoff, individual SQL adapters, SpiceDB tuple validation, or model accuracy.
Those remain with their owning Rust crate or Python package. Compose-shape
checks remain in `//infrastructure/docker/tests:test_compose`; this suite tests
running behavior rather than YAML structure.

Failures preserve `docker compose ps` and service logs in Bazel test output.
Compose resources are always removed in teardown, including on assertion or
startup failure.
