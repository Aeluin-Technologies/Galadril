# Plugin GOL sources

Law and Utils ship bounded `.gol` source files. Their manifests contain package
identity/version, source paths, capability requirements and optional editor binding
metadata. The catalog transports `files: [{path, text}]`; it contains no JSON
ontology graph. Python neither parses GOL nor resolves imports or inheritance.
Registry uses the shared Rust compiler to validate and publish these sources.
Imports use explicit module aliases to avoid collisions between local labels and
imported type names. Local permissions are `read_access` and `write_access` because
`read` and `write` are reserved GOL words; their external bindings remain `read`
and `write`.

The Law modules are `law.socle`, `law.ecritures`, `law.moyens`, `law.preuves`,
`law.contentieux`, `law.administratif`, `law.judiciaire` and `law.penal`. Previously
inherited fields are explicit declarations in the authored branch sources. They
do not assert an unsupported GOL subtype relationship. Legal
fields carry Restricted classification and the Legal compartment. Authenticated
identities remain external to those business types.

Utils provides `communication.people`, `communication.content`,
`documents.attachments`, `communication.threads` and `communication.mail`. Its
Instant import is an explicit registered external contract, `gal.time.Instant`
version 1.0.0. Sender/to/cc/bcc mapping slots and address role metadata are plugin
editor hints, not authorization policies or another ontology model. Changing a
mapping requires editing and validating GOL through Registry. Bcc visibility must
also be enforced by the admitted security profile; editor metadata grants no access.

Capability contracts use canonical selectors such as
`communication.mail::Message`. Source-level imports pin module versions. Only
Registry resolves those imports and records compiled generation dependencies.
Install and publish Law dependencies before their importing packages. Registry
returns a dependency-unavailable diagnostic when an approved module/version is
missing; the Python catalog does not fetch or flatten those modules.

Installing sources, publishing them, and binding a pipeline block remain separate
authorized operations. There is no historical JSON overlay materializer.

# Runtime consumption

Vision opens `/internal/gol/runtime` through the configured trusted Gateway
endpoint. A dedicated opaque admission, minted at an authenticated workload
boundary, is required in addition to tenant, pipeline revision and block identity.
Kafka attributes and caller-supplied classifications cannot create admissions.
Gateway finalization replaces source metadata with `authz-gol-admission`. Intake
transports it in the dedicated Avro Authz field; Vision removes it from ordinary
record payloads and passes it only in the command admission field. Runtime requests
also bind `sourceObjectId` to `raw:` plus the exact S3 key, preserving percent
encoding. A mismatch with Intake's trusted resource is rejected. Missing admission
fails closed. The response contains authorized symbol descriptors
and JSON Schema projections pinned to one generation, never full source or IR.

The Python `galadril_ontology` package contains transport DTOs and an HTTP client.
It has no semantic ontology model, compiler, import resolver, storage reader, or
security decision engine. An active scope retains an opaque server calculation;
a ContextVar is transport plumbing and proves no isolation. The Gateway retains
IFC provenance across related steps and rechecks live policies before effects.

ESKG keeps its existing Entity/Event/State and structural relation vocabulary.
Business classifications record module, exact version, source revision, generation,
symbol selector and GOL kind. Entity and Event refer to GOL entities; State refers
to a GOL field. Existing evidence cannot be relabeled by a later generation.
The database sink requires explicit `gol_symbol_key`, `gol_event_symbol_key`,
`gol_edge_symbol_key`, `gol_entity_state_symbol_key`, and `gol_embedding_symbol_key`
parameters. Each item supplies an explicit `gol_value`; the record supplies its
event `gol_value`. Utils emits typed Person and Message values alongside ESKG
metadata. The sink submits every exact value for trusted host validation and fresh
effect admission before opening its database client. Proposals are bounded to
128 KiB. Unknown invariants remain a host validation refusal. Generic inference
adapters must provide an explicit mapping instead of guessing ontology fields.

Eru derives extraction vocabulary solely from the authorized projection. Binary
relation extraction uses explicitly named `source` and `target` roles; it never
infers direction from reference order. Unsupported n-ary relations are excluded
from that binary extraction vocabulary. Scribe receives data-only projections:
server context handles and admissions never enter model prompts. Evidence memory
requires transfer permission before inference and write permission before storage.

# Verification boundaries

Python unit tests check source transport, bounds, immutable generation references,
missing admissions, revocation and the absence of sensitive sink effects after a
refusal. They do not substitute for the shared Rust semantic/security tests or
interservice tests with the real authorization and storage engines. In particular,
a JSON Schema or an extraction vocabulary proves neither business invariants nor
permission to execute an action or ingest a proposed instance.

Evidence indexing requires `gol_derived_symbol_key` identifying an authorized
derived declaration whose value shape is `{source_id, facts}`. Facts contain
`subject`, `predicate`, and `object`; the host must validate the declared contract
and applicable invariants. Missing or incompatible contracts refuse persistence.
Index metadata retains generation/source/context provenance separately from the
business value. This does not constitute a transaction spanning Gateway and SQL;
the server conservatively retains the effect destination label before returning
a receipt, including failed outcomes.

# Effect and release provenance

The Gateway–Vision channel additionally requires an operator-owned 32-byte worker
credential at `GOL_WORKER_KEY_PATH`. It is distinct from job admission and receipt
signing keys. Gateway verifies the unique worker header before parsing a request;
a browser holding a job handle cannot act as the dispatcher. Plugins and browser
containers do not receive this file. The Vision client reads the bounded file for
each call so key rotation is not an indefinite cached grant.

Every actual write supplies its exact business proposal and logical instance ID.
An `allowed` response without a signed receipt is insufficient for persistence.
ESKG stores `gol_value` and `gol_receipt` as system provenance, separate from
business fields. The signature binds the instance, target, generation, source
revision, value digest and information-flow restrictions. A receipt establishes
integrity and provenance; it does not replace fresh authorization. Context handles,
admissions and receipts must be removed before public serialization or prompts.

New receipts group field obligations by ontology, instance, revision and
operation. The signed representation retains every logical dependency and is
limited to 4,096 dependencies and 64 KiB on the worker transport. Gateway
continues to read earlier signed receipts and rechecks their original obligations.

Shared vector, identity and causal state require `gol_state_symbol_key`; derived
writes additionally require `gol_derived_symbol_key`. Missing routes refuse I/O
before a SQL transaction or native mutation. When native state lacks complete
permission provenance, a conservative classification floor is necessary but does
not prove access to the underlying dependencies. Such results must remain
unreleasable unless the host establishes every required dependency.

Each pipeline step declares `gol_transfer_symbol_key` for broker release. Results,
lineage, successors, retries and dead letters each receive a fresh `transfer`
check against destination `kafka`, purpose `pipeline_dispatch`, before publication.
A cached completion retains its original admission and immutable execution tuple;
authorizing a new filtered projection cannot authorize old cached field contents.
Gateway must revalidate the calculation's entire historical dependency set,
including fields no longer present in a new projection. A refusal after durable
completion does not turn the operation into a failed execution or rerun its writes.
Malformed ingress has no verified release scope, so its raw payload and parser
errors are not copied to a quarantine topic. Cron events without a trusted source
admission are also refused; a timer is not proof of independent provenance.

The Gateway decision and SQL/Kafka effect are separate operations. A revocation
between their completion cannot be made transactionally atomic by this adapter.
The tests distinguish transport and sink-boundary unit cases from actual
interservice authorization tests. Native engine and production image verification
also require hermetic dependency builds; failing dependency construction is not a
successful E2E test and must be reported separately.

# Retained effect scopes

A permission precheck does not protect a later SQL, HTTP or Kafka operation from
concurrent changes to the calculation label. Workers use `protected_gol_effects`
for one bounded batch, or its single-effect and shared-state helpers. The host
retains the admission's exclusion gate from `begin` until the worker reports
completion after the actual I/O. Every effect and exact proposed value is checked
before that batch starts. Required write receipts are returned in the same order.
An `Allow` without a permit cannot reach a sink.

Failure or cancellation reports unsuccessful completion; missing completion and
permit expiry invalidate the admission before the host releases its gate. No
client can reset that computation. A nested adapter may reuse only an exact
previously approved effect, including operation, target, destination, purpose,
instance, proposal and receipt. It cannot extend a live batch. This exclusion
boundary protects calculation provenance; it is not a transaction spanning
SpiceDB, Gateway, PostgreSQL and Kafka.

New event and evidence-memory identities are scoped to the admitted raw source:
`gol:<sha256(sourceObjectId)>:<sha256(localId)>`. Existing resolved entity IDs
remain unchanged and require existing authority and signed provenance. A missing
permission cannot be repaired by taking ownership of an existing instance.
Memory indexing does not prune prior units whose independent deletion obligations
are not established by the write batch.
