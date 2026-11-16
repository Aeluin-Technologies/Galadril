# Contextual IFC and executable verification

The existing GOL authorization gate now tracks information actually used by
each calculation. SpiceDB and every Cedar policy layer remain mandatory. IFC
adds obligations; it grants no additional access.

## State and effects

`Clearance` is an admission ceiling, not a source label. A `Computation`
starts with complete, trusted input provenance. Reads require
`current_label join data_label <= max_clearance`, then raise `current_label`.
Writes require `current_label <= destination_label`. High-clearance users can
write public data when their calculation has only public inputs. Reading
confidential data prevents a subsequent dependent public write.

The join takes the higher of Public, Internal, Confidential and Restricted,
unions compartments and intersects purpose and destination allowlists.
`None` adds no restriction; an explicit empty allowlist denies all releases.
Writes preserve these restrictions as well as level and compartments. There
is no reset, general declassification or privileged expurgation API.

`ExecutionContext` binds state to the authenticated issuer, tenant, subject
type and subject ID. Dependent tasks share it or use `fork`, which inherits
all restrictions and cannot erase failure. Failed observations poison the
calculation: neither effects nor label snapshots can turn their existence
into lower output. `Evidence.max_clearance` supplies the current IAM ceiling
at each check, so a cached admission cannot override a clearance revocation.

Construction performs accounting, not isolation. The trusted dispatcher must
include values, captured state, control dependencies, caches, errors and
absence results. A new request, task ID or caller assertion does not prove
independence. Unknown dependencies need conservative restrictions. Arbitrary
host Rust or Python is not sandboxed by this API.

Approved Cedar resource attributes contribute `Descriptor::control_flow`
before evaluation, including denied and erroneous outcomes. Instance floors
strengthen these inputs. `Evidence.context_flow` covers trusted time,
location, MFA and other context; these inputs are not automatically public.
Approval to load policy attributes is a TCB decision, not an end-user read
permission. A sink unable to receive the control label is rejected before
evaluation. Metadata filtering can reject a statically out-of-clearance
candidate without consulting its value or policy, preserving the same public
schema as an unknown candidate.

Action propagation follows preconditions, postconditions, read effects and
intermediate reference hops of write paths. It no longer raises a write
destination's classification from action inputs or turns blind writes into
reads. Every write is rechecked after all read prerequisites, regardless of
route ordering. A sensitive source cannot silently reclassify a public
destination to conceal a prohibited copy.

`perform` holds the calculation through the actual trusted host effect. The
local and remote archive readers use the same gate with archived routes and
need no parser. Whole-symbol and compound field writes are rejected: a
parent grant cannot replace child checks. Callbacks must be bound to the
checked resource and destination, use accurate effect declarations, and use
transactional storage where required. Nested admission on the same context
is forbidden while its effect is held. Revocation is checked at admission;
there is no distributed transaction with IAM or SpiceDB.

An opaque `EffectReceipt<T>` retains the destination restrictions on success,
payloads and storage errors. Inspection requires fresh permissions, the
originating calculation, current clearance and a suitable sink. Field
mutation results require the corresponding read route. Remote expiry blocks
admission but does not cancel an already admitted backend mutation, whose
outcome must remain labelled. Hosts still own cancellation and crash recovery.
OTLP-compatible tracing emits bounded categories without values, labels,
subjects, context or backend error text.

## Verus boundary

Production and `proofs/ifc.rs` import the same executable
`src/authorization/kernel.rs`. Official Verus syntax attributes disappear in
normal Rust builds. The pinned verifier is `0.2026.10.04.426d8b0`, using Rust
1.98.1. `//platform/ontology/rust:ifc_verus_test` runs
`--no-cheating --compile`; a missing or different verifier fails. Set
`VERUS_BIN` for installations outside PATH. No `assume`, `admit`,
`external_body` or replacement implementation bypasses verification.

The proofs cover numeric join bounds and lattice laws, read clearance,
monotonic state, sticky failure, numeric no-write-down, and conjunctive
obligations. A public calculation remains writable independently of high
maximum clearance. These are universally quantified properties of executable
functions. Dynamic `BTreeSet` compartments and allowlists, async locks,
runtime evidence, dependency collection, storage and policy engines are
outside the proof. Their dimensions enter the numeric kernel through a
predicate checked by ordinary Rust. Generated tests cover all 262,144 pairs
in a 512-label fixture lattice and deterministic triples.

This does not prove application-wide non-interference or process isolation,
or verify SpiceDB, Cedar or FlatBuffers. The design follows the distinction
between clearance and current labels in [LIO](https://arxiv.org/abs/1207.1457)
and uses [official executable Verus specifications](https://verus-lang.github.io/verus/guide/).

## Production integration audit

The audited checkout has no authoritative binding between published GOLC
contracts, deployed business properties and authenticated IAM clearances.
These effect paths remain **outside contextual GOL IFC enforcement**:

| Entry point | Existing boundary | Missing IFC contract |
| --- | --- | --- |
| Gateway `application/usecases/authorization.rs` | Loth and tenant Cedar checks | Clearance, calculation admission and labelled outputs |
| Gateway graph `explore.rs`, `relations_age.rs` | Tenant and relationship filtering | Property labels, view/control provenance and release hooks |
| Registry `domain.rs` | Immutable revisions, publication and binding CAS | Authoritative GOLC-to-property mapping and label revision |
| Vision `compute/tasks.py::sink_to_db_batch` | Tenant batch, transaction and outbox | Labelled inputs/control and destination checks before writes/publication |
| Vision PostgreSQL `graph.py::ensure_vertex_on_connection` | Tenant, structural kind, immutable ontology reference | Field destination labels and labelled write results |
| Gateway `chat_tools.rs`, plugins `utils/tools.py` | Tool leases and delegation | Transfer labels for model/tool inputs, errors and results |
| Ray tasks and model/retrieval caches | Service-specific isolation | Complete shared-state/value/control provenance |

ESKG business-type classification and workflow privacy booleans are not
confidentiality labels. This change does not reinterpret them, invent IAM
clearance or classify unlabelled resources as Public. Until the authoritative
mapping and isolated dispatcher are specified and connected, **Phase 9 is
incomplete at application level**. Required inputs are the clearance and
compartment authority, Registry property-to-route mapping, and durable
provenance across Python/Ray/Kafka/cache boundaries. Storage must enforce the
same label revision and destination at commit that were checked at admission.

## Compatibility and validation

IFC semantics are version **2.0.0**. Archived route records require
`control_flow` and `compound`; older records fail closed. Recompile and
republish old artifacts through the existing atomic publication protocol.
The binary container framing version is unchanged.

FlatBuffers unsafe code stays confined to `golc-wire`, with only the checked
root helper exported. Handwritten code forbids unsafe Rust. Generated code
and the FlatBuffers verifier remain trusted dependencies.

The Docker fixture uses pinned RustFS and SpiceDB, official Cedar and this
IFC gate. It checks actual stored contents, denied public copies, successful
protected writes, action ordering, clearance failure, cached context changes,
ZedToken revocation, receipts and forbidden LLM/unregistered Kafka sinks. A
separate process admitted before sensitive observations receives fixed public
inputs and no parent values or control messages. This demonstrates one
bounded independent calculation, not arbitrary dispatcher isolation.
No Kafka broker or production Gateway/Vision IFC adapter is exercised.
The [validation record](gol_ifc_validation.md) lists commands actually run and
their results.
