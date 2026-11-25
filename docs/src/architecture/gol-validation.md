# Runtime invariant validation

The checked expression model lives in `galadril_ontology::checked`. The semantic
compiler and syntax operator paths re-export the same types, preserving existing
callers and expression archive records. Runtime builds with `archive`, `export`,
and `remote` do not need the parser to validate an admitted proposal.

## Contracts in compiled archives

Each entity, struct, and relation has an explicit `v/{owner}` contract, including
owners with no predicates. The contract pins the ontology version and retains
checked invariant bodies in deterministic definition-name order. Type aliases
are normalized only in these contracts; the original canonical declarations and
`e/{expression}` records retain their identities and encoding.

`ArchiveReader::invariant_contract` and `RemoteArchive::invariant_contract` first
reuse the existing live read/write gate. They check the exact owner and version,
verify the authenticated block, and bound the byte range before fetching the
payload. A missing or corrupt contract is unavailable, never an empty invariant
set. Denial and integrity failure share the same public result. These methods
return private compiler metadata to trusted validation code; an HTTP or GraphQL
handler must not expose the contract, predicate names, or paths to clients.

## Admitted facts and supported expressions

`validation::Bindings` supplies facts already verified by the host. Field lookup
receives the complete sequence of owner-pinned members and the explicit base,
which distinguishes two occurrences of the same struct and different relation
roles. Context facts come from a trusted component. The evaluator neither
traverses references nor performs I/O to obtain missing values.

The pure evaluator supports Boolean, character, string, exact Rust integer widths,
finite floating-point values, and nominal enum equality. Integer operations are
checked, including narrow-width overflow and division by zero. Floating-point
arithmetic rejects nonfinite results; floating-point ordering is unsupported.
Both operands of Boolean operations are checked, so a true disjunction cannot
hide a missing binding or unsupported dependency. Strings and enum variants are
borrowed during evaluation.

An unresolved reference, direct role, action parameter/result, optional value,
collection, or external comparison returns a structured rejection. A field
reached from a role can be supplied by the host using the complete role base and
path. Serialization does not confer comparison capabilities. The host's shape
validator establishes enum membership and admissible JSON representations;
`Contract::check` independently requires exact scalar types and enum identities.

## Bounds and failure behavior

The defaults are hard ceilings: 64 KiB encoded payload, 64 nested JSON containers
and AST/type constructors, 4,096 structural nodes/operators/path members, and
8,192 evaluation steps. Trusted callers may lower the ceilings. Bounded text
comparisons also consume execution steps. The JSON container bound can reject
an expression before it reaches the AST depth bound because enum encodings add
containers. The official `serde_json` recursion limit remains enabled.

Decoding allocates bounded contract storage. Checking borrows that storage and
host values without allocating intermediate values. This does not establish a
real-time latency guarantee for remote reads, external policy engines, host fact
resolution, or the surrounding application. OTLP tracing records the invariant
check span without recording values or private predicate names.

## Host obligations

A passing invariant is not authorization to modify an instance. The host must
still verify live SpiceDB and Cedar obligations, IFC dependencies and destination
constraints, tenant and generation pins, reference existence, authorized current
state, and the atomic effect boundary. A host must refuse missing bindings and
unsupported capabilities; it cannot convert an unavailable contract into an
unconditional acceptance.

These checks prove neither all GOL business rules nor non-interference of every
Galadril computation. External adapter comparisons, general functions, action
execution, recursive graph evaluation, and automatic reference traversal remain
outside this bounded kernel.
