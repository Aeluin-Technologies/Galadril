# Authorization, tenant isolation, and lineage

## Previous model and threats

Gateway verified JWTs and usually checked Loth, but WebSockets used hard-coded
identities and permission strings had no repository contract. Intake accepted
mutable tags, fell back to a tenant parsed from an object key, and preserved a
payload-supplied `authz` object. Vision trusted Kafka tenant fields and could
materialize arbitrary relationships. Database provisioning did not enable RLS;
Gateway's tenant transaction set `search_path` but not `app.tenant_id`; and
background causal/cache paths omitted tenant identity.

Those gaps enabled forged tenants, BOLA/IDOR, confused-deputy processing,
cross-tenant cache/graph access, permission drift, and RLS bypass.

## Invariant and trust boundaries

Authentication identifies the actor; trusted claims resolve the tenant;
SpiceDB authorizes a domain permission; Cedar may further deny using trusted
context; and PostgreSQL RLS independently limits every row read/write.

External users write only to staging with a short-lived URL. Gateway checks
`tenant:ingest`, copies into the tenant prefix, and replaces security metadata.
Destination-bucket IAM must allow writes only from Gateway. Intake accepts only
object-store notifications on an ACL-protected topic, requires Gateway-authored
scoped metadata, overwrites document `authz`, and publishes on topics whose only
producer is Intake. Vision rejects missing/mismatched context and materializes
only owner-approved relationship categories.

Intake has no public HTTP policy surface: its ingress is the object-store
notification topic. That is still a security boundary. Only the object store
may publish notifications, and only Gateway may write the destination bucket.

Kafka ACLs, destination-bucket IAM, mTLS/service authentication, and distinct
SpiceDB tokens are part of the boundary. `X-Internal-*` headers are not trusted.

## Security context and execution identity

Protected work carries request/trace ID, normalized tenant, actor type/ID,
authentication issuer, execution kind (`user_delegated` or `service`), target,
permission, optional ZedToken, and origin/delegation. User metadata is separate
and cannot overwrite this context.

Async work records the initiator even under a service execution identity.
Revocable or destructive work re-checks permission at execution; historical
allow flags are invalid. Tenant is part of cache, idempotency, graph, and
deduplication keys.

## PostgreSQL and SpiceDB lifecycle

There is no cross-system transaction. Vision persists tenant data plus an
idempotent authorization-outbox row atomically. Vision owns delivery and
reconciliation. New resources stay non-addressable until authorization exists.
Deletion removes external access first, then data, with a reconciliation
tombstone for partial failure.

Normal services use non-owner, non-superuser, non-`BYPASSRLS` roles.
Reconciliation and maintenance use separate identities and entry points.
`SET LOCAL app.tenant_id` immediately follows `BEGIN`; missing context denies,
and transaction end clears state before pool reuse.

Gateway uses one `DATABASE_URL`, verifies the normal role and every tenant
table's forced RLS at startup, and refuses unsafe configuration. Vision uses `user/password`
for tenant work and requires separate `maintenance_user/maintenance_password`
for schema or authorization-outbox reconciliation. Production secrets must not
reuse the development credentials in `examples/pipeline.yaml`.

Arbitrary permission-record mutations do not exist. New grants must target a
typed resource relation owned by one service.

## Contextual Cedar restrictions

Tenant administrators can install syntax-validated Cedar policies through
`setCedarPolicy`. Cedar runs only after SpiceDB allows. The example in
`schemas/spicedb/examples/contextual-constraints.cedar` restricts a signed
`data_engineer` role to 08:00–19:00 UTC and the signed `Europe` region, and
requires the signed `internal` device posture for `person1`. Role, region, and
device posture must be issued by the identity provider; request headers are not
accepted as policy facts. Production deployments should replace region strings
with stable IdP-defined security zones if legal geography semantics are needed.

## Audit

Emit `authorization.check`, `authorization.relationship.write`,
`security.context.accepted`, `security.context.rejected`, and
`tenant.transaction`. Include request/trace, tenant, actor/service identity,
resource, permission, decision, consistency, relationship operation, and
ZedToken when available. Never include bodies, prompts, credentials, tokens, or
arbitrary payload metadata. Security audit storage is access-controlled and
retention-managed independently from debug logs.

## Required `loth` changes

The pinned interface needs per-check consistency (`minimize_latency`,
`at_least_as_fresh`, `fully_consistent`), `checked_at` in check results,
`written_at` in relationship acknowledgements, and a hard SpiceDB-AND-Cedar
invariant. Local Gateway code evaluates Cedar only after a SpiceDB allow until
that interface is available.

Gateway membership writes still use Loth's in-memory replication queue. It
retries and fails closed, but it is not a durable transactional outbox. Moving
tenant/role membership lifecycle writes to a durable Gateway outbox, with
`written_at` acknowledgements, remains required before claiming crash-atomic
PostgreSQL/SpiceDB lifecycle convergence for IAM administration.

## Developer guide

1. Add the permission to `schemas/spicedb/schema.zed` and document semantics
   and its sole relationship writer.
2. Add a typed application permission; never add a raw string check.
3. Resolve tenant from trusted authentication/delegation and require a
   tenant-qualified target ID.
4. Check SpiceDB before side effects; apply Cedar only as a restriction; fail
   closed on denial, timeout, malformed context, or policy errors.
5. Use a tenant transaction for SQL. Add RLS `USING` and `WITH CHECK`, force
   RLS, and test two tenants, missing context, and pool reuse.
6. For dual writes, choose one relationship owner, use an outbox/compensation,
   make writes idempotent, and define reconciliation plus ZedToken semantics.
7. For background work, carry initiating and execution identities, bind tenant
   into ACLs/idempotency keys, and state whether execution re-authorizes.
8. Add positive/negative SpiceDB behavior tests, forged/missing/cross-tenant
   tests, dependency-failure tests, and structured audit assertions.

## Deterministic security tests

Run the complete deterministic suite with:

```sh
bazel test //platform/vision/tests/security:authorization_defense_in_depth
```

The suite uses fixed-version Postgres, SpiceDB, and Kafka images. Gateway tests
install the current schema into a real Postgres instance, then prove concurrent
tenant transactions cannot observe each other, pool reuse clears `SET LOCAL`,
forged writes fail, missing context sees no protected rows, and explicit system
queries remain usable while tenant transactions are active. SpiceDB tests load
the canonical schema, exercise permissions rather than relationship internals,
and verify immediate revocation and cross-tenant denial. Kafka tests send valid
and forged security envelopes through a real broker into Vision's production
normalizer. Intake unit and integration tests independently reject every
missing, forged, or mismatched security-context field.

All fixtures use fixed IDs and bounded readiness checks. Each container has a
fresh datastore; no test depends on execution order or external state.

A literal process-level Intake -> Vision model -> Gateway test is not currently
possible under the three-container constraint. Intake production ingress also
requires S3 notifications and Schema Registry, while Vision model execution
requires Ray and model artifacts; there is no repository adapter that connects
the three service binaries in-process. The deterministic suite therefore tests
each real security boundary without mocking Postgres, SpiceDB, or Kafka. To add
the literal pipeline test, first introduce narrow injectable adapters for the
object notification/schema lookup and model executor, then drive the same
signed fixture through all three services. Do not add more infrastructure to
this suite merely to simulate those boundaries.
