# Pipeline configuration, isolation, and revision control

## Storage responsibilities

TerminusDB is the production authority for pipeline definitions, publication
references, ontology overlays, ontology branches, and version history.
PostgreSQL remains responsible for IAM, audit events, conversations, pipeline
execution ledgers, AGE graphs, and vectors. S3 holds raw data, models, and staging
uploads. Pipeline configuration is no longer published to S3.

Gateway and Intake use the shared asynchronous Rust HTTP adapter in
`platform/versioning`. Vision and `platform/ontology` use an asynchronous HTTPX
adapter. Both use the native `TerminusDB-Data-Version` header for optimistic
writes and return server-generated commit identifiers. There are no SQL history
writes in these production versioning adapters and no PostgreSQL fallback.

TerminusDB has an [official Python SDK](https://github.com/terminusdb/terminusdb-client-python)
and a [community Rust SDK](https://github.com/ParapluOU/terminusdb-rs). Direct async
HTTP keeps the wire contract consistent across both languages and avoids running
synchronous SDK calls on Vision's event loop. The adapters use the documented
[version-control API](https://terminusdb.org/docs/version-control-operations/).
DoltgreSQL was not selected: document snapshots fit the ontology/pipeline model,
and PostgreSQL must remain for the existing extensions regardless.

## Configuration files

| File | Purpose | Consumers |
| --- | --- | --- |
| `examples/connectors.yaml` | Trusted credentials, Gateway listener, Ray and connector settings | Gateway, Intake, Vision |
| `examples/pipeline.example.yaml` | Example `name`, `sources`, and `pipeline` DAG | Explicit local Vision demonstration or authoring input |
| TerminusDB tenant databases | Native snapshots and published references | Gateway, Intake, production Vision |

`examples/pipeline.yaml` was split into these two YAML files. `version` is also
an accepted pipeline field. Gateway and Vision reject tenant-authored connector,
Ray, and deployment-identity settings. Secrets belong to trusted connector
configuration; all checked-in credentials are development values.

Gateway uses `GALADRIL_PIPELINE_PATH` (retained for compatibility), Intake uses
`INTAKE_BOOTSTRAP_PATH`, and Vision uses `VISION_BOOTSTRAP_PATH` or
`--bootstrap-config`. Defaults point to `examples/connectors.yaml`. Compose
mounts it at `/connectors.yaml`.

`connectors.terminusdb` contains `endpoint`, `organization`, an explicit `tenants`
map, and a separate `bases` capability. Each tenant entry supplies a database,
user, and password. Tenant identities are exact keys; they are never interpolated
into database paths. Missing mappings fail closed. Configure only the tenant
capabilities that a deployment needs. Use read-only database capabilities for
Intake and Vision runtime deployments, and a separate base-writer capability
for release initialization. Do not distribute the administrator password to
application containers.

## Provisioning and isolation

Compose includes TerminusDB 12.0.7 with persistent storage. The development init
service creates `tenant_a`, `tenant_b`, and `bases`, then grants separate users
capabilities scoped to their database. It verifies both cross-tenant reads fail
with HTTP 403 before dependent services start. Existing resources are retained.
The administrator password is used only by the server and provisioning job.
The TerminusDB port is bound to loopback for local development.

```sh
docker compose -f infrastructure/docker/docker-compose.yaml up -d terminusdb terminusdb-init
```

Production provisioning must create equivalent database-scoped capabilities,
use secret-managed connector files, restrict network access, and provide TLS
when crossing an untrusted network. Every tenant database must retain the
`terminusdb:///data/` document base used by the adapters. Duplicate tenant
mappings to the same database and administrator usernames are rejected locally.
Server capabilities protect direct access as well as branch and historical
reads; branches are versioning workspaces within one tenant, not tenant boundaries.
See [TerminusDB access control](https://terminusdb.org/docs/access-control/).

Gateway still performs identity, Cedar/SpiceDB authorization, and durable audit
checks. Pipeline publication does not transfer connector credentials. One
Vision service loads all publications for the trusted tenant capability map.
Intake publishes a separate record for each matching publication and attaches
the exact tenant, pipeline, and revision as Kafka headers. Vision indexes
ingress by that complete immutable identity plus source. Missing, duplicate,
malformed, or payload-conflicting identity metadata fails closed; cross-tenant
fallback is forbidden.

All tenants share one set of Kafka consumer groups and CPU, GPU, and causal Ray
actor pools; actor-local database, object-store, model, and ontology resources
are reused. Ontology bindings use the stable tenant pipeline ID, so a new
revision preserves the binding lookup.

## Native pipeline lifecycle

Each tenant's `main` branch holds current pipeline and catalogue documents.
Every edit creates a native immutable commit. Pipeline HEAD is the tenant branch
snapshot version; unrelated catalogue changes in that tenant can therefore
cause an optimistic edit to fail. Clients must reload and explicitly retry.
No write is automatically retried over concurrent changes.

Publishing atomically stores the selected native commit ID in the pipeline's
publication pointer. A later draft edit preserves that pointer. Readers load
`pipeline/<id>` from that immutable commit, never from the current draft.
Deletion atomically clears publication and records a tombstone; native historical
snapshots remain available. Publication and deletion also create native commits,
so callers must retain the HEAD returned by the latest operation.

Intake refreshes its routing cache after at most five seconds. Vision pins all
published pipelines for all tenants declared in the trusted connector file:

```sh
bazel run //platform/vision/src/galadril_vision:vision -- \
  --bootstrap-config /etc/galadril/connectors.yaml \
  --role all
```

An inaccessible or malformed configured tenant catalogue fails startup. Startup
also fails when the configured tenants have no publications. Publishing does
not mutate a running route table. Drain and restart the shared Vision deployment
to load the next consistent publication set; deleting a pipeline does not stop
an already-running pinned revision.

For an explicitly selected local example:

```sh
bazel run //platform/vision/src/galadril_vision:vision -- \
  --bootstrap-config examples/connectors.yaml \
  --pipeline-config examples/pipeline.example.yaml --role all
```

Compose runs this local example. It does not publish it for a tenant, and Intake
has no routes until a tenant pipeline is created and published through Gateway.

## Ontology history and initialization

`platform/vision` remains the canonical code-defined base. Register releases
once in the shared base database, then initialize tenant overlays through the
existing ontology application service using the new repository:

```python
from galadril_ontology.backends.terminus import (
    TerminusClient,
    TerminusOntologyRepository,
)
from galadril_vision.ontology.base import initialize_vision_ontology


async def initialize(config):
    client = TerminusClient(config.connectors.terminusdb)
    try:
        repository = TerminusOntologyRepository(client)
        service = await initialize_vision_ontology(repository)
        branch = await service.initialize_tenant("tenant_a")
        return branch.head_revision_id
    finally:
        await client.close()
```

Tenant branches are native TerminusDB branches named `ontology-<hex name>`;
user-facing names are limited to 59 ASCII characters. Tenant snapshots store
sparse overlays, base hashes, and semantic provenance, without duplicating the
full base ontology. Effective ontologies are reconstructed and validated from
immutable snapshots and the pinned shared artifact.

The application retains field-level diff, tombstones, dependency validation,
and semantic conflict detection. A successful semantic merge is committed as
a native squash snapshot; its provenance records the accepted source commit.
Subsequent merges recognise that source snapshot. This is not a native two-parent
Git merge commit. Unresolved conflicts are persisted without advancing the
ontology branch. Publications and bindings live in the tenant catalogue and
resolve immutable validated ontology commits at runtime.

## Cutover, compatibility, and verification

PostgreSQL migrations and security initialization are retained for operational
data, vector indexes, execution state, and AGE graphs. PostgreSQL is not a
versioning backend and contains no ontology history tables.

Existing S3 pipelines and PostgreSQL history are **not automatically imported**.
For a populated deployment, pause authoring and workers, export the existing
published definitions and sparse ontology states, recreate them through the
TerminusDB-backed application services, and record the old-to-native revision
mapping. Republish and rebuild bindings against those native IDs before starting
Intake and replacement Vision deployments. Keep the old database and S3 objects
until this cutover is verified. Old UUID references are not valid native commits
merely because they pass identifier-shape validation.

Back up both TerminusDB (including the shared base database) and PostgreSQL.
Audit and content writes cross databases and do not form a distributed
transaction. An interrupted request may have committed content before its
success audit or response was persisted; inspect native state and reconcile the
recorded attempted operation before retrying. Do not promise rollback by
switching configuration to PostgreSQL: this implementation has no backend toggle.

Transport reads have a 30-second timeout and a 16 MiB response bound. Current
catalogue reads load a tenant snapshot; capacity and latency must be measured for
large tenant catalogues before deployment. Bound breaches fail closed.

Run `bazel test //...`. The native integration target
`//platform/ontology/tests:test_terminus_integration` uses the pinned server and
checks branches, immutable reads, optimistic conflicts, semantic merges,
publications, slices, and direct cross-tenant access denial. It requires Docker.
The Rust transport target `//platform/versioning:terminus_integration_test`
uses the same real server to verify pipeline document writes, immutable commit
reads, native history, stale-write rejection, and tenant capability enforcement.
Unit tests exercise wire contracts, server error mapping, deployment isolation,
and actor-local HTTP lifecycle without Docker. A passing mock suite does not
replace the real-server isolation gate.
