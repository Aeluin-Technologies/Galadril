# GOL worker effect boundary

Gateway admits a calculation only after authenticated upload authorization. Its
input is bound to the final immutable raw-object key, the staged ETag and trusted
S3 metadata. The source channel's deployment floor initializes `current_label`;
IAM clearance is only its ceiling. Missing channel approvals deny admission.
Interactive MFA is not inferred from device trust or renewed by a worker request.

An opaque admission retains one computation through fan-out, retries and block
changes. Registry supplies the exact published pipeline/block binding. An open
reader keeps its generation; a calculation cannot silently substitute another
generation of the same package or dependency.

## Actual I/O

A worker submits its complete effect batch before opening the transaction or
external call. Gateway holds the calculation's exclusion guard from admission
until completion. Every host path that can observe an input acquires the same
guard. All batch inputs and outcome labels are joined before checking writes, so
a later read cannot invalidate an earlier public-write precheck.

The Python protected scope encloses the actual SQL transaction, Kafka publication
or HTTP stream. A nested operation can reuse an outer scope only when its entire
operation, target, destination, purpose, instance and proposed value already occur
in the admitted batch. It cannot extend that batch or reenter the host lock.

Failure, cancellation or an expired 30-second permit invalidates the calculation
before releasing its guard. This does not roll back an effect already committed
by another system. It prevents an uncertain outcome from continuing in a fresh,
lower-labeled context. Admissions expire after 15 minutes and are process-local;
Gateway restarts or routing to a different replica fail closed. Resumable
cross-replica calculation state is not implemented.

## Revocation and values

Every new effect rechecks current IAM, raw-source ownership/version, pipeline
permission, the complete retained dependency set, and the active GOL security
contract. A cached or filtered projection cannot erase old dependencies.

HMAC receipts bind tenant, logical type, instance, generation, source revision,
complete JSON value, IFC flow and original dependency obligations. They attest
provenance, never an authorization decision. Reading a receipt still requires
current permissions and destination checks. The separate deployment signing key
is required; it is not derived from public ontology fingerprints.

New instance grants use source-scoped identifiers and an atomic SpiceDB
`MUST_NOT_MATCH` precondition. Existing relationships are never touched, including
when ingestion retries after revocation. Resolved shared identities retain their
own grants. Typed ingestion values must remain immutable on replay; generic
instance patching needs authoritative current state and is not implemented here.

## Conservative limits

Cross-owner traversals without authoritative instance bindings, opaque shared
native state without complete provenance, and executable action requests are
refused. A source channel or caller assertion cannot reclassify that state.
Mixed-classification compound writes can be refused after the conservative batch
join; this boundary does not prove independent dependencies between those writes.

This documents the worker boundary, not completion of all service migration.
The final workspace build and interservice E2E acceptance remain separate checks.
