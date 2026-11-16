# GOL canonical model

`galadril-ontology` is the Cargo library for the Galadril Ontology Language
(GOL). It lives in `platform/ontology/rust` alongside the Python ontology
client and builds under `//platform/ontology/rust:ontology`. The public Rust API is
re-exported from `galadril_ontology`. It uses portable metadata and the
workspace's existing `semver` and `tracing` dependencies.

## API and dependency direction

| Internal modules | Public model |
| --- | --- |
| `identity`, `named`, `ontology` | `Ontology`, `ModuleVersion`, `OntologyId`, `OntologyVersion`, `Name`, `QualifiedName`, `SymbolKey`, `SymbolRef`, `SymbolId`, `SymbolKind`, `Named<T>`, `Import`, `ImportSelection` |
| `declaration`, `value_type` | `Declaration`, `Entity`, `Relation`, `Role`, `Field`, `FieldKind`, `Attribute`, `Cardinality`, `Primitive`, `TypeRef`, `TypeAlias`, `StructType`, `EnumType` |
| `external_type` | `ExternalTypeId`, `ExternalTypeRegistry`, `ExternalTypeDescriptor`, `ExternalSchema`, `TypeCapability`, `AdapterContract` |
| `expression` | `Reference`, `FieldRef`, `Expression`, `TypedExpression`, `Predicate`, `Invariant`, `Derivation`, `View`, `Action`, `Effect`, `Idempotence` |
| `security` | `SecurityContract`, `AccessBinding`, `RelationshipPermission`, `ContextPolicy`, `IfcLabel`, `Classification`, `FlowContract`, `TransferConstraint` |
| `error` | `ModelError` |

Compiler, security validation, artifact format and storage components will
consume the canonical model. The model's references point to declarations or
external contracts by logical identity. The default Cargo feature
`parser` enables the separate [source grammar](gol_grammar.md).
Consumers of the canonical model can disable default features.

## Identity and composition

- `SymbolKey` combines the logical ontology ID and the local declaration name.
  It is stable across reorderings and versions. Renaming or moving a declaration
  changes the key. The compiler will decide how to diagnose replacement by an
  incompatible declaration under the same key.
- `SymbolRef` pairs that key with an exact semantic version. The version pin
  includes prerelease and build metadata. `resolve_local` checks the ontology
  and version before looking up the declaration. Consumers can inspect the
  declaration's `SymbolKind` to check suitability for the reference site.
- `SymbolId` is a dense index scoped to a future compilation table. Declarations
  use logical keys and version pins. Hashing, ID allocation, and physical
  locator formats remain open.
- Canonical names currently use case-sensitive ASCII identifier segments with
  dots for qualification. Phase 2 owns source spelling, Unicode and escape
  rules. The module has one namespace across declaration kinds. Within a
  relation, roles and fields have separate named scopes.
- An import pins one exact version per target ontology and records the selected
  names. A future linker can consolidate source imports, check the selection,
  and resolve references against a pinned module universe. The source of any
  lock and its representation remain open.

Maps and sets order independent declarations, imports, named members, and
contract sets by their keys. Two otherwise equal models compare equal across
input permutations. Stable byte encoding is a separate format decision.
`Named<T>`, import assembly and external registration reject duplicate keys
without replacing entries. Constructors check simple names, qualified names
and cardinality bounds. The model can describe forward and cyclic business
references. Validation of target kinds, field legality, and recursive value
types belongs to semantic analysis.

Registry still owns tenant context, immutable revisions, and publication.
An `OntologyVersion` is the version declared by GOL, while Registry and ESKG
pin server-owned immutable revisions in the existing tenant-scoped contract.
A future integration must map these explicitly. See [Registry](ontology.md)
and the [ESKG contract](eskg.md).

## External types and security

The host explicitly registers an `ExternalTypeDescriptor` keyed by an exact
`ExternalTypeId`. A descriptor can carry a schema and fingerprint, a canonical
value encoding, size and depth limits, and an adapter implementation identity
and version with a host ABI requirement. Capabilities start empty. Validation,
projection, equality, ordering, and indexing are explicit and independent
declarations. Schema metadata and codec support confer none of them.
The core stores these values and checks registry uniqueness. Adapter trust,
compatibility checking, encoding and resource limits require later host
integration. Chrono, chrono-tz and Jiff can be supplied through future adapters.

A type or field can retain a versioned `SecurityContract`. Each protected
operation binds a relationship permission and a contextual policy action.
The relationship contract pins the schema and resource bridge. The contextual
contract pins the policy bundle. An IFC label reference and sink/purpose
restrictions can be attached independently. A field's requirements and its
type's requirements are both retained. A future validator checks that all
required obligations are present, and the runtime needs to enforce them
together. An absent contract and a public IFC classification carry no grant.

IAM supplies authenticated subjects, accounts and groups. SpiceDB owns
relationship tuples and grants. Cedar evaluates predicates over resource
attributes and trusted request context. IFC governs classification and flows,
including release after a successful read. A business `Person` is a domain
type. A GOL `Ref<Person>` refers to that domain type and provides no way to
construct a principal, grant or authorization decision. The core performs no
policy linking or evaluation, label propagation, or transfer enforcement.

The library reports local construction rejections as bounded reason codes in
the host's `tracing` subscriber. Galadril binaries can export them through the
existing OTLP bridge. The library does not install a subscriber or initiate
network I/O. Logs contain no input name, schema text or policy details.

## Handoff to parsing and semantic analysis

`Expression` carries a definition identity and declared data and control
dependencies. `TypedExpression` and `Predicate` specify the expected result
type. They carry no expression body. An action describes parameters, result,
preconditions, postconditions, effects and retry guarantees. These are
declarations the host must validate. The model has no evaluator, callback,
adapter loader, or way to execute an action.

The parser produces an ordered AST with source spans. [Semantic analysis](gol_semantics.md)
collects symbols, resolves imports and checks types before lowering declarations
into this model. The checked project retains expression bodies and local
security linkage alongside the canonical modules. Source locations stay in the
diagnostics. Validation against external authorities belongs to the next phase.

For publication, the mission specifies source history in lakeFS and active
`.golc` artifacts and operational manifests in RustFS. Older compiled
generations can coexist temporarily for reader consistency during publication.
The future container will use indexed, independent FlatBuffers blocks.
Block placement, dependency indexes and range locations belong to the format
and storage components.

Run the library tests and executable Rustdoc example with
`cargo test -p galadril-ontology` or
`bazel test //platform/ontology/rust:all`.
