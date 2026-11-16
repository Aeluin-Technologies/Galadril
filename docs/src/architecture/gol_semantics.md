# GOL semantic analysis

`galadril_ontology::semantic::analyze_project` accepts the existing
`SyntaxTree` values and an `Environment`. The environment carries a
local external type registry, the types of trusted context properties and the
default list bound. The compiler consumes these inputs synchronously. It
returns a `Project` on success or a set of located, stable diagnostics.
The Cargo feature `parser` enables parsing and semantic analysis together.

## Passes

1. **Collect modules and declarations.** A compilation accepts one exact
   SemVer version per ontology ID. Each module has a namespace shared by its
   top-level declaration kinds. Access blocks and policies are keyed by
   their resolved targets in a later pass. Every named declaration in the
   current syntax is exported.
2. **Build import scopes.** The analyzer binds absolute paths, selected exports
   and aliases against the collected modules and the host registry. Each
   scope indexes visible spellings by hash lookup. Source and module order
   have no effect on name resolution.
3. **Resolve signatures.** Resolve field and action parameter types,
   relation roles, aliases and cardinalities across all modules. An
   iterative depth-first traversal rejects cycles in by-value definitions.
4. **Check bodies.** Resolve property paths and enum values and check
   constraints, projections and action clauses against the complete
   signature table. Reject cycles among derived fields.
5. **Bind security targets.** Collect access permissions first, then resolve
   guards, labels and policies. Check GOL policy conditions in the target
   scope and validate their types against the host-provided context shape.
6. **Assemble the project.** Emit the Phase 1 `Ontology` values and
   source-independent metadata. Dense `SymbolId` values follow the sorted
   `SymbolRef` order and are scoped to this compilation. Logical
   `SymbolKey` values remain stable across source and version changes.

Each pass gathers independent errors before it stops the pipeline. A later
pass requires valid inputs from the earlier passes, which avoids cascades
from missing modules or types. Diagnostics sort by source ID, byte range and
stable code. Duplicate module declarations and conflicting import spellings
include a secondary location when available. The diagnostics implement
miette and may be presented with caller-supplied source text. Rejection
telemetry contains only the stable error code.

## Imports and names

`import lib;` admits both `Item` and `lib.Item`.
`import lib::{Item};` admits only the selected symbol.
`import lib as items;` exposes `items.Item` and `lib.Item`.
`import lib::Item as Entry;` exposes `Entry` and `lib.Item`.
Ambiguous spellings and reused module qualifiers are diagnosed. Multiple
imports of the same module normalize to one pinned dependency with the union
of the selected names. An omitted module version uses the single version
supplied in the project. An explicit version must match exactly.

External imports use `import type`. They resolve the namespace and type
name in the registry and select an exact descriptor version. An unpinned
external name must have exactly one candidate. Backend adapter linkage remains
pending. A plain external qualified path is visible only through an import.

## Type rules

- `Ref<Entity>` carries an exact entity reference. Cyclic module imports
  and cycles through such identity references are valid. Structural cycles
  through aliases, structs, `Option` or `List` are rejected under the
  current value-type rules.
- Relations have one or more uniquely named roles with entity participants.
  Different roles may use the same entity type. Cardinalities are unsigned
  `u32` bounds with an inclusive upper bound or an unbounded marker.
  An explicit lower bound above the upper bound is rejected.
- Type aliases are transparent for compatibility. Structs and enums are
  nominal. An enum variant resolves through a qualified enum name or the
  expected enum type where the expression supplies one.
- Integer and float literals take a compatible expected numeric type from the
  declaration or an operand. The defaults are `i64` and `f64`. The
  analyzer checks literal ranges, including signed minima, and rejects
  non-finite float literals. Typed values keep their declared widths.
- Arithmetic requires matching numeric types and remainder requires integers.
  Boolean operators and predicates require `bool`. Equality is supported
  for scalar values other than `Unit`, enum values and compatible
  entity references. Ordering is supported for integers, strings and chars.
  Float ordering awaits an explicit policy. External equality and ordering
  require the corresponding descriptor capability.
- Property paths resolve every hop against an entity or structured value.
  Scoped relation expressions can traverse singleton roles. Optional and
  collection values need an explicit future access operation. External
  schema projection awaits a contract that supplies checked member types.
- A view projects a record and can filter it with a Boolean expression.
  Computed outputs require explicit names. A derivation supplies the value
  of a declared field at that field's type. An action checks Boolean
  preconditions and postconditions, effect paths rooted in declared
  parameters and the name of any deduplication parameter. The action result
  is visible in postconditions. Keys and derived fields cannot be written.

These rules deliberately limit Phase 3 to locally checkable contracts. View
composition, recursive value semantics, projected external schemas and
collection traversal remain open. Runtime arithmetic, idempotence and
instance cardinality enforcement belong to later components.

## Canonical result and handoff

The canonical modules use the Phase 1 `Ontology`, `Declaration`,
`TypeRef` and `SymbolRef` types. The project also owns checked
expression bodies keyed by the canonical `Expression::definition`,
resolved effect paths, IFC transfer declarations and security metadata.
Field paths are stored flat. Effects retain their action parameter roots,
even when two parameters have the same type and the same canonical field
target. Checked expressions contain resolved types and symbols without
retaining the AST.

Module dependencies are available in each canonical module's pinned imports
and the project's exact external imports. The declaration dependency set
distinguishes value types, business references, expression and effect inputs,
and security obligations. These edges can support forward and reverse
indexes in a later format.

Security metadata retains the target, declared SpiceDB permission and Cedar
action names, checked contextual predicates, resolved field guards and IFC
labels. A local contract can be valid while external linkage is
`PendingExternal`. Invalid contracts produce diagnostics. The
`Project::validation` accessor also reports pending adapters and named
host effects. The [security compiler](gol_security.md) consumes both the
modules and this metadata to link against pinned external schemas and approved
adapters. Publication authorization remains with its owning service.

The [mail example](../../../platform/ontology/rust/tests/fixtures/semantic_mail.gol)
and its [attachment module](../../../platform/ontology/rust/tests/fixtures/semantic_attachments.gol)
exercise mutually importing ontologies, named and external types, typed
constraints, an action and pending security bindings.
