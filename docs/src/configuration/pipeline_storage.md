# Registry storage configuration

Application services configure the internal Registry gRPC endpoint at the top
level of `connectors.yaml`:

```yaml
registry:
  endpoint: http://registry:50052
```

Registry reads `connectors.s3` from the same file as other services. The
`connectors.s3.bucket` must match its service-owned storage namespace. Raw
lakeFS repository names, S3 paths, and storage namespaces are never accepted
from gRPC callers. Registry also receives these deployment settings:

| Variable | Purpose |
| --- | --- |
| `LAKEFS_ENDPOINT` | Internal lakeFS API endpoint |
| `LAKEFS_ACCESS_KEY_ID` | Registry-only lakeFS credential |
| `LAKEFS_SECRET_ACCESS_KEY` | Registry-only lakeFS secret |
| `REGISTRY_STORAGE_NAMESPACE` | Bucket-root S3 namespace used for tenant partitions |
| `REGISTRY_REPOSITORY_PREFIX` | Prefix for hashed tenant repositories |
| `REGISTRY_CONFIG_PATH` | Shared `connectors.yaml` path |
| `REGISTRY_BIND_ADDR` | Registry gRPC listen address |

Docker Compose runs lakeFS with S3 block storage backed by MinIO and mounts the
same connector file into Registry, Gateway, Intake, and Vision. Kubernetes mounts
the shared connector file from the `galadril-connectors` Secret and keeps lakeFS
credentials in `registry-lakefs`. Callers reference the `registry:50052` Service.

## Tenant lifecycle

`ValidateTenants` accepts one to 100 explicitly supplied, syntactically valid
tenant IDs and reports whether each `<tenant>/_registry/tenant.json` marker
exists in S3. It never enumerates tenants, and an empty request is invalid.
Positive and negative marker reads use a bounded, ten-second Moka cache; tenant
creation and deletion update or invalidate that cache. The registry does not
query PostgreSQL. `GetTenant` and every artifact read fail when the marker is
absent. `PutTenant` and artifact insert/update RPCs idempotently initialize an
opaque lakeFS repository and S3 marker before their write path.

The bucket layout is tenant first:

```text
<tenant>/raw/<group>/<file>
<tenant>/_registry/tenant.json
<tenant>/_lakefs/<lakeFS-managed blocks>
```

Inside the tenant lakeFS repository, Registry stores logical artifacts at
`ontology/state.json` and `pipeline/state.json`. The `_lakefs` physical prefix
is private to lakeFS; its block keys do not mirror logical repository paths.

`DeleteTenant` is reserved for GDPR erasure. The request must repeat the exact
tenant ID in `confirmation_tenant_id`; Registry then removes the lakeFS
repository record and the complete `<tenant>/` S3 prefix, including raw data and
the tenant marker. Deleting an ordinary pipeline or ontology never invokes this
tenant purge.

The direct S3 permission exists because deleting a lakeFS repository does not
delete its physical objects. Registry does not use S3 to bypass lakeFS for
ontology or pipeline reads and writes.

## Revisions and publication

Every successful Registry mutation creates a lakeFS commit. The immutable commit
ID is the opaque revision returned to clients. Updates supply an expected
revision, publications pin an exact revision, and Vision must request the same
revision carried by its Kafka execution identity.

There is no application history API. Operators must not expose lakeFS commit
logs as ontology or pipeline history. A future PostgreSQL history model will
provide searchable audit semantics independently of storage versioning.

## Initialization

No deployed state exists, so there is no legacy database migration or compatibility
reader. Discard prototype databases and author the desired ontology documents,
pipeline definitions, publications, and bindings through Registry; their first
accepted writes establish the lakeFS revisions. The unshipped PostgreSQL
pipeline prototype migration is removed rather than carried forward. Gateway
migrations must never create ontology, pipeline, publication, or binding tables.
