# Multi-Tenant Ontology

## Decision

The `galadril-ontology` library owns ontology resources, tenant overlays,
validation, immutable revisions, branches, semantic diffs, merge conflicts,
materialization, and persistence contracts. `platform/vision` remains the
canonical code provider for the platform base ontology.

PostgreSQL relational tables are the authoritative store for the initial
revision graph. Apache AGE is not authoritative and is not populated by the
initial implementation.

This design was selected after considering three storage models:

| Model | Decision | Reason |
| --- | --- | --- |
| PostgreSQL relational tables | Selected | Composite keys, foreign keys, RLS, immutable rows, and branch compare-and-swap all share one transaction. Recursive CTEs are adequate for a tenant-scoped revision history. |
| PostgreSQL plus an AGE projection | Deferred | A disposable projection may improve visualization or very large ancestry queries later, but it would add synchronization work without improving current correctness. |
| AGE as the primary topology | Rejected | Branch-head concurrency, cross-tenant parent constraints, migrations, and immutable revision enforcement are simpler and stronger in relational tables. |

The ontology revision DAG is separate from the AGE entity graph. Ontology
branch merging never merges business entities, entity lineage, or pipeline
nodes.

## Canonical State

The canonical state consists of:

1. An immutable base artifact stored once for each Vision release.
2. Immutable, tenant-scoped revisions containing semantic changes.
3. Ordered, tenant-scoped parent edges.
4. Lightweight branch rows pointing to revision heads.

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

Branches contain only a name and head revision. A commit inserts its revision,
parents, and optional materialization, then moves the head with a SQL
compare-and-swap:

```sql
UPDATE ontology_branches
SET head_revision_id = :new_head
WHERE tenant_id = :tenant
  AND branch_name = :branch
  AND head_revision_id IS NOT DISTINCT FROM :expected_head;
```

If no row is updated, the whole transaction is rolled back and the caller
receives a concurrent-head error. A stale writer can never silently replace a
newer head.

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
before a two-parent revision and its branch-head move are committed.

A conflict records the resource ID, semantic path, conflict kind, and explicit
base, target, and source value states. The structure is suitable for a future
editor, API, or agent-assisted resolution flow.

## Isolation and Database Invariants

All tenant-owned tables have `tenant_id` in their primary key. Parent and
branch references use composite foreign keys containing the same `tenant_id`,
making cross-tenant ancestry and branch heads unrepresentable. PostgreSQL RLS
is enabled and forced for revisions, parents, branches, conflicts, and cached
materializations using the established `app.tenant_id` session setting.

Base artifacts are global platform data. They are registered through the
maintenance path and read by tenant sessions. Tenant resources exist only
inside tenant changes and materializations; another tenant cannot address the
revision that contains them.

## Validation

Every base artifact, commit, synchronization, and successful merge validates
the materialized ontology. Validation covers stable identifier syntax,
duplicate identifiers, resource-kind requirements, dangling owners and
references, invalid property value types, and owner cycles. Additional
action/function and runtime constraints can be registered in the library
without coupling it to an LLM provider.

## Materialization

The canonical overlay can always be rebuilt from the first-parent chain.
Disposable materializations store the accumulated sparse overlay and validated
effective ontology for a revision. Normal reads therefore perform a keyed
lookup rather than replaying the complete history. A missing or deleted cache
entry is deterministically reconstructed from immutable revisions and the
single referenced base artifact.

This cache boundary also gives downstream AI and pipeline systems a stable API:

```text
materialize(tenant_id, revision_id) -> validated effective ontology
```

No LLM provider is part of the ontology domain model.

## Production Runtime Publications

PostgreSQL is also the sole runtime source of truth. A tenant can register
multiple stable `ontology_id` values in `ontology_catalog`; an ontology's
immutable revision is promoted through `ontology_publications`. A partial
unique index permits exactly one `production` publication per tenant and
ontology while retaining prior publication metadata.

`pipeline_ontology_bindings` maps `(tenant_id, pipeline_id, block_id)` to one
tenant-owned ontology and a non-empty semantic selector. Different blocks in a
pipeline may bind different ontologies, and the same tenant may operate any
number of pipelines and ontologies without sharing mutable pointers.

Before each Vision block executes, the actor queries PostgreSQL for its current
binding and production publication. A recursive SQL query extracts only
resources selected by stable identifier or resource kind, plus referenced and
owned dependency resources when requested. The resulting `OntologySlice`
contains publication, revision, base, effective-hash, and binding metadata. It
is validated against ontology invariants and the processing block's resource
contract, then bound with `ContextVar` for the duration of that asynchronous
block invocation. Mutable publication state is not cached in the actor, so a
new production promotion is visible on the next block execution.
