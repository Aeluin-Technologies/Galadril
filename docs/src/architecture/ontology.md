# Ontology and pipeline Registry

Registry is the single persistence and semantic authority for tenant ontologies,
pipelines, publications, and pipeline-to-ontology bindings. Gateway and Vision
use the versioned `galadril.registry.v1.Registry` gRPC contract. Neither service
receives lakeFS credentials, repository names, object paths, or storage
namespaces.

Registry maps each validated tenant identity to an opaque, server-owned lakeFS
repository and path. lakeFS stores immutable revisions while S3 remains the
physical object store. A lakeFS commit ID is returned unchanged as an opaque
Registry revision ID and is used for optimistic writes, publication pinning,
and reproducible Vision execution.

Tenant lifecycle is also owned by Registry. Internal callers may validate only
a bounded, explicitly supplied list against Registry-owned S3 markers. Empty
validation requests are rejected and Registry exposes no tenant enumeration
operation. Reads fail closed for missing tenants; artifact insertions
initialize the server-derived S3 marker and lakeFS repository when necessary.
The tenant deletion RPC requires an exact repeated tenant ID and exists solely
for irreversible GDPR erasure of both lakeFS metadata and physical S3 objects.

The current service deliberately contains no mTLS or application authentication
implementation. A deployment proxy will authenticate service identities and
authorize these internal RPCs; Registry still validates every payload and
storage-derived identity instead of treating the future proxy as input
validation.

## Semantic ownership

For every immutable revision, Registry reconstructs the ontology graph and
pipeline DAG from the stored artifact state. Its canonical Rust implementation:

- validates resource identities, kinds, owners, links, and value types;
- resolves inheritance, interfaces, references, and dependency closures;
- detects cycles, dangling references, and incompatible inherited properties;
- validates pipeline nodes, edges, step types, and ontology bindings;
- performs resource-aware semantic diffs and three-way merges.

Inheritance and interface edges form a directed acyclic graph. For example,
`object.pilot` may extend `object.person`: the effective Pilot type inherits
`property.person.name` and adds its own
`property.pilot.flight_history`. Dependency-closed slices include the selected
type, its ancestors, and properties owned by each type, so Vision receives the
complete effective contract without implementing inheritance itself.

Semantic merges never invoke lakeFS file-level merge. A successful merge is
validated and committed as a new Registry revision; a conflicting merge returns
structured conflicts without changing storage. Derived graphs may be cached by
revision ID, but the cache is disposable because every graph is reconstructible
from its immutable revision.

## Ownership boundaries

Gateway authenticates users, applies SpiceDB and Cedar authorization, preserves
PostgreSQL RLS, and records audit/access events. Audit rows may refer to opaque
Registry revisions, but PostgreSQL does not persist ontology or pipeline
artifacts.

Vision workers load an explicitly selected tenant pipeline and block-local
ontology slices by tenant, pipeline, block, and immutable pipeline revision.
They retain the
FastStream, Kafka, and Ray execution architecture and never accesses lakeFS.

Registry intentionally exposes no history-listing API. lakeFS commits are an
internal versioning primitive, not application history. Searchable and auditable
artifact history will be designed separately in PostgreSQL.

## Initialization boundary

This architecture has not been deployed, so there is no legacy data importer
and Registry contains no legacy database read path. Prototype state is discarded;
ontologies, pipelines, publications, and bindings are authored afresh through
Registry and committed directly to lakeFS. The unshipped PostgreSQL pipeline
prototype migration is removed; Gateway migrations contain only audit/access
and unrelated application tables.
