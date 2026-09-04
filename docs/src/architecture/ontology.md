# Multi-Tenant Ontology

## Decision

The `galadril-ontology` library owns ontology resources, tenant overlays,
validation, immutable revisions, branches, semantic diffs, merge conflicts,
materialization, and persistence contracts. `platform/vision` remains the
canonical code provider for the platform base ontology.

TerminusDB is the sole authority for versioned ontology snapshots and native
branch references. PostgreSQL retains operational data, vector indexes, and AGE
entity graphs, but contains no ontology history or publication tables.

See [configuration, isolation, and native version control](../configuration/pipeline_storage.md)
for tenant database provisioning, semantic squash merges, publication, and cutover.

The ontology revision DAG is separate from the AGE entity graph. Ontology
branch merging never merges business entities, entity lineage, or pipeline
nodes.

## Canonical State

The canonical state consists of:

1. An immutable base artifact stored once for each Vision release.
2. Immutable, tenant-scoped revisions containing semantic changes.
3. Native commit history and immutable semantic merge provenance.
4. Native TerminusDB branches pointing to revision heads.

An effective ontology is derived as follows:

```text
effective(tenant, revision)
    = base_artifact(revision.base_version)
    + accumulated_sparse_overlay(first_parent_history(revision))
```

The base artifact is generated from code in `platform/vision`. Its canonical
SHA-256 hash detects packaging or deployment mistakes where a release name is
reused with different content. The database stores one artifact per platform
release, not one copy per tenant. A historical revision pins both the release
identifier and hash, so it remains reproducible after application upgrades.

## Resource Identity

Every resource has a stable `resource_id` independent of its display name.
Properties are first-class resources with their own stable IDs and an
`owner_id`, for example:

```text
core.customer
core.customer.name       owner=core.customer
core.customer.email      owner=core.customer
```

Renaming a display name or changing a description does not change identity.
References, owners, pipelines, actions, functions, and future authorization
bindings use the stable identifier.

## Sparse Tenant Overlay

Tenant revisions contain domain operations rather than effective snapshots:

```text
add_resource
set_field
remove_field
remove_resource
restore_field
restore_resource
```

`remove_resource` creates an inherited-resource tombstone. It does not delete
the base resource. `restore_resource` removes all tenant state for that
resource, while `restore_field` removes only the selected field override.

The accumulated overlay distinguishes:

- tenant-owned resources;
- inherited resource tombstones;
- field values explicitly set by the tenant;
- field tombstones explicitly removed by the tenant.

Anything absent from the overlay is inherited. This means a future base change
to an untouched field appears after synchronization without rewriting the
tenant's ontology or disturbing unrelated overrides.

## Revisions and Branches

Revisions are immutable and have zero, one, or two parents. The first parent
defines the overlay replay path. A second parent records merge ancestry.
Because parent revisions must already exist before a new immutable revision is
inserted, cycles cannot be introduced through the supported API.

Branches are native TerminusDB references. A write supplies the expected native
`TerminusDB-Data-Version` and atomically commits the sparse state and advances
the branch. A stale writer receives a concurrent-head error. The adapter returns
the server-generated commit identifier instead of the provisional domain UUID.

## Base Evolution

Base synchronization creates an ordinary immutable revision whose parent is
the current tenant head and whose base reference points at the newer artifact.
The sparse overlay is applied unchanged:

- untouched fields inherit the new base;
- explicit tenant values continue to win;
- tombstones continue to suppress inherited resources or fields;
- an override of a resource removed by the new base becomes a structured
  synchronization conflict;
- the resulting effective ontology must pass full validation.

This is the synchronization operation equivalent to rebasing tenant changes
onto a new platform base. It can be run automatically during rollout without
copying the platform ontology into tenant rows. Historical revisions continue
to use their pinned base artifact.

## Semantic Merge

Merge uses the closest common ancestor in the tenant revision DAG and
materializes three effective ontologies: base, target, and source. Resources
are compared by stable identity and fields are compared by semantic paths.

For each value:

- equal target and source values are accepted;
- a value changed on only one side is accepted;
- different changes to the same value conflict;
- deletion against an unchanged value accepts the deletion;
- deletion against a modification produces a delete/modify conflict;
- two different additions with the same stable ID conflict.

The merged effective ontology is converted back to a sparse overlay relative
to its pinned platform base. This preserves future inheritance instead of
turning the merge into an opaque snapshot. The merged ontology is validated
before a native squash commit advances the branch. Immutable semantic provenance
records the accepted source revision; it is used to recognise repeated merges.

A conflict records the resource ID, semantic path, conflict kind, and explicit
base, target, and source value states. The structure is suitable for a future
editor, API, or agent-assisted resolution flow.

## Isolation and Database Invariants

Each tenant owns a separate TerminusDB database with database-scoped
capabilities. Native branch and commit paths are resolved only through trusted
configuration. Unknown tenants and cross-database reads fail closed. Shared base
artifacts live in a distinct database accessed with a separate capability.
PostgreSQL RLS continues to protect operational data; versioning no longer relies
on SQL revision tables or composite foreign keys.

## Validation

Every base artifact, commit, synchronization, and successful merge validates
the materialized ontology. Validation covers stable identifier syntax,
duplicate identifiers, resource-kind requirements, dangling owners and
references, invalid property value types, and owner cycles. Additional
action/function and runtime constraints can be registered in the library
without coupling it to an LLM provider.

## Materialization

Each native snapshot contains the accumulated sparse overlay and the expected
base/effective hashes. Reads reconstruct and validate the effective ontology
against the immutable shared base. Tenant snapshots do not duplicate the entire
base ontology. Historical reads use the native commit path.

This cache boundary also gives downstream AI and pipeline systems a stable API:

```text
materialize(tenant_id, revision_id) -> validated effective ontology
```

No LLM provider is part of the ontology domain model.

## Production Runtime Publications

A tenant can register multiple stable ontology IDs in its TerminusDB catalogue.
Each catalogue document contains one current publication pointer; native history
retains previous publication states. Pipeline block bindings select an ontology
and a non-empty semantic selector from the same tenant database.

Before each Vision block executes, its actor resolves the binding and production
publication from a consistent catalogue snapshot, loads the pinned ontology
commit, and selects resources and dependency closure. The resulting
`OntologySlice` contains publication, revision, base, effective-hash, and binding
metadata. It is validated against ontology invariants and the block's resource
contract, then bound with `ContextVar` for that asynchronous invocation. Mutable
publication state is not cached in actors.
