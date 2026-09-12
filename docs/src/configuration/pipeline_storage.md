# Registry storage configuration

Application services configure only the internal Registry gRPC endpoint and,
for event-driven runtimes, an explicit trusted tenant set:

```yaml
connectors:
  registry:
    endpoint: http://registry:50052
    tenants: [tenant_a, tenant_b]
```

Raw lakeFS repository names, S3 paths, and storage namespaces are never accepted
from gRPC callers. Registry alone receives the following deployment settings:

| Variable | Purpose |
| --- | --- |
| `LAKEFS_ENDPOINT` | Internal lakeFS API endpoint |
| `LAKEFS_ACCESS_KEY_ID` | Registry-only lakeFS credential |
| `LAKEFS_SECRET_ACCESS_KEY` | Registry-only lakeFS secret |
| `REGISTRY_STORAGE_NAMESPACE` | Server-owned S3 namespace used for repositories |
| `REGISTRY_REPOSITORY_PREFIX` | Prefix for hashed tenant repositories |
| `REGISTRY_S3_ENDPOINT` | S3-compatible endpoint used for tenant markers and GDPR purge only |
| `REGISTRY_S3_ACCESS_KEY_ID` | Registry-only S3 credential scoped to its namespace |
| `REGISTRY_S3_SECRET_ACCESS_KEY` | Registry-only S3 secret |
| `REGISTRY_S3_REGION` | S3 request-signing region |
| `REGISTRY_BIND_ADDR` | Registry gRPC listen address |

Docker Compose runs lakeFS with S3 block storage backed by MinIO. Kubernetes
deploys Registry with separate `registry-lakefs` and `registry-s3` Secrets;
callers reference only the `registry:50052` Service.

## Tenant lifecycle

`ValidateTenants` accepts a caller-supplied list of one to 100 syntactically
valid tenant IDs and reports whether each Registry marker exists in S3. It does
not enumerate other tenants. `GetTenant` and every artifact read fail when that
marker is absent. `PutTenant` and artifact insert/update RPCs idempotently
initialize an opaque lakeFS repository and S3 marker before their write path.

`DeleteTenant` is reserved for GDPR erasure. The request must repeat the exact
tenant ID in `confirmation_tenant_id`; Registry then removes the lakeFS
repository record, all physical objects under its server-derived S3 prefix, and
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
