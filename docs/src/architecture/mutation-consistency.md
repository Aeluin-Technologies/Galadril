# Mutation consistency boundaries

This audit follows the mutation families exposed by Gateway's GraphQL schema
through their application services and PostgreSQL adapters. A resolver delegating
to one service is not, by itself, proof that the underlying operation is atomic.
Likewise, a SQL transaction cannot roll back lakeFS, SpiceDB, object storage,
Polis or SMTP effects.

## Corrected binding operation

`bindPluginOntology` previously saved `ontology_required` through
`PipelineService::author`, then independently called `PutBinding`. It now calls
one authorized control-plane operation. The adapter sends the new
`AuthoringCommand::BindOntology` through Registry's existing pipeline command RPC.

Registry loads one tenant snapshot, validates the existing block, its published
ontology and selector, modifies the draft and binding in memory, then commits the
whole state with one expected revision. No external write occurs before that
commit. Approval is cleared; the existing deployed release remains pinned.
An older Registry rejects the unknown command before mutation. Generic public
pipeline authoring rejects this internal command so it cannot bypass the binding
authorization and audit path.

`binding_and_required_contract_share_one_atomic_revision` exercises an unavailable
ontology, a stale head, an injected storage commit failure and a successful write.
Rejected operations leave both the definition and binding list unchanged. The
previous immutable revision also retains its original state.

## PostgreSQL and delegated mutation review

| Mutation family | Observed boundary | Recovery or remaining limitation |
| --- | --- | --- |
| Conversation/message updates and generation reservations | `database/conversations.rs` uses one tenant-scoped SQL transaction for messages, attachments, revisions and reservation state | SQL errors roll back the dependent SQL writes. Streaming/model calls have their own durable generation states. |
| Passkey reset | `database/sso.rs::reset_passkeys` increments the credential generation and deletes credentials and challenges in one transaction | Failure cannot leave only part of this reset committed. |
| Pengolo record, retention and retrieval-job writes | `database/pengolo.rs` passes the same transaction into its record writer and related SQL changes | A single commit owns each dependent SQL group. |
| Pengolo upload promotion and deletion | A durable reservation/retention record precedes object effects; promotion checks owner, revision and expiration under a row lock | Object effects are not rolled back by SQL. Retry and retained cleanup state are part of this workflow, not distributed ACID. |
| IAM membership and hierarchy changes | `database/iam.rs` combines local SQL writes with tenant lifecycle/advisory locks; grants occur only after an approved SQL commit, revocations precede removal | The native authorization projection is a separate system. Failure ordering is conservative, but a crash or projection failure still needs reconciliation. Existing deferred-commit tests verify that a failed grant commit publishes no external grant. |
| Project permissions | `save_project` serializes metadata and projection under the tenant lifecycle lock; reads also consult current metadata | Partial SpiceDB projection fails closed. It is not an atomic SQL/SpiceDB commit; automatic reconciliation is still needed for convergence after a crash. |
| Pipeline and ontology creation/publication | Registry owns immutable metadata; Gateway subsequently installs initial ownership relationships | This creation-to-ACL sequence remains non-atomic. A durable authorization projection is needed to recover orphaned or partially projected resources. The atomic binding fix does not fix these separate operations. |
| Legacy conversation creation | `ConversationService::create_conversation` commits PostgreSQL, then installs parent/owner relationships | A failed projection can leave a durable conversation that its creator cannot open. There is no demonstrated durable projection replay in this path. |
| Legacy direct-upload completion | `S3Uploader::finalize_object` copies the object and removes staging before Gateway installs the raw-resource relationships | An authorization failure does not undo the copy or subsequent notifications. Deleting the destination as compensation would be unsafe once ingestion has started. |
| Role synchronization configuration — corrected | `PgFederationStore::configure_approved_roles` now creates approved role names and their mapping configuration in the same tenant transaction, reusing IAM quota, identifier and tombstone validation | A deferred configuration-commit failure rolls back both objects. Native role relationships are projected after the SQL commit; this separate system still needs durable reconciliation. |
| SSO provider configuration | Tenant domain ownership is reserved before Polis is changed, then the connection reference is persisted | Polis searches the tenant's existing connection before updating, supporting retries without changing identity IDs. This still needs a durable operation/reconciliation record for interruption between systems. |
| General access and resource grants | Configuration is validated before writing a policy; `AccessStore::set_many` submits a bounded relationship batch in one native SpiceDB write | Atomicity of a native batch does not extend to a separate PostgreSQL metadata write. |
| Audit | Attempt records and terminal outcomes are separate durable audit writes | Attempts intentionally survive rejected business transactions. An audit record alone is not a transactional outbox or evidence of successful recovery. |

The two direct pool queries found in `Database::verify_security` inspect role and
RLS configuration; they are reads. Tenant business SQL uses `Database::tenant`,
which begins a transaction and installs `app.tenant_id` with transaction-local
scope. The audit found no reason to replace those existing local transaction
boundaries with a transaction opened in `schema.rs`.

`approved_roles_and_configuration_rollback_together_on_commit_failure` installs a
deferred PostgreSQL trigger that rejects the configuration commit. It verifies
that neither the new role nor the mapping remains, then removes the fault and
checks successful creation and tenant isolation against real PostgreSQL.

## Rule for dependent writes

1. Keep dependent writes under the domain owner in one command. Pass one SQL
   transaction through repository helpers instead of committing inside each
   helper. For Registry, mutate one snapshot and perform one compare-and-swap
   commit. A GraphQL request containing multiple mutation fields is not an atomic
   domain command.
2. Across stores, persist an explicit operation or transactional outbox with the
   authoritative write. Record the intended projection, operation identity,
   tenant, revision and completion state before applying external effects.
3. Replay must be idempotent and fenced against newer state. In particular, a
   delayed grant must not restore access that has subsequently been revoked.
   Never reuse an earlier authorization decision as proof for a later write.
4. Report success only at the documented completion boundary. Distinguish an
   accepted, pending projection from a completely applied operation.
5. Compensate only effects that remain safe to reverse. Retain durable recovery
   state for failed compensation and ambiguous outcomes. Sent mail, published
   evidence and ingested objects cannot be repaired by a blind delete.

The remaining cross-store rows above are explicit follow-up findings, not claims
of atomicity or implementations of a general distributed transaction manager.
