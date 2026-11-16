# GOL source grammar

[gol.pest](../../../platform/ontology/rust/src/syntax/gol.pest) is the grammar
used by the embedded Rust parser. The syntax follows the declaration shapes
in GOL v0.3 and specifies the lexical and expression rules left open by the
draft. A `.gol` source contains one or more `ontology` blocks. Each block
declares a module with a qualified name, a quoted version and declarations in
any order. The AST retains that order and leaves version validity, symbol
uniqueness, visibility and reference resolution to semantic analysis.

## Lexical rules

Identifiers use case-sensitive ASCII letters and underscores followed by ASCII
letters, digits or underscores. This matches the canonical model's name
segments. A dot separates segments of an ontology, type, target or import
path. In expressions, `::` qualifies a symbol such as `Status::Archived`
and a dot selects a property such as `message.status`. Both forms preserve
their segments and positions.

Spaces, tabs, CR and LF separate tokens. Comments use `//` through the end
of a line or a non-nested `/* … */` block. These may appear between
tokens. Strings use double quotes, and characters use single quotes and
contain one Unicode scalar. Both support UTF-8 and the escapes
`\"`, `\'`, `\\`, `\n`, `\r`, `\t`, `\0` and
`\u{H…}` with one to six hexadecimal digits. Raw control characters
require escaping. Surrogate values and values above U+10FFFF are rejected.
Decimal literals preserve their spelling. A float has digits after the
decimal point, a signed or unsigned exponent, or both. A leading minus is
a prefix operator.

### Reserved words

```text
ontology version import type entity relation attribute struct enum
key field owns role invariant on derive view from project as where
action requires ensures effects read write emit retry idempotent
non_idempotent deduplicated security label classification compartments
integrity destinations purposes access resource permission cedar permit
forbid when unless
true false Option List Ref Unit bool char String
i8 i16 i32 i64 i128 u8 u16 u32 u64 u128 f32 f64
usize isize unsafe fn loop while for let mut
```

The last line reserves unsupported Rust forms. IFC classification spellings
`Public`, `Internal`, `Confidential` and `Restricted` are contextual
and can also be used as ordinary identifiers, for example in an enum.

## Declarations and types

| Form | Example |
| --- | --- |
| Module header | `ontology industry.assets version "1.0.0" { … }` |
| Module import | `import industry.assets version "1.0.0";` |
| Selected symbols | `import industry.assets::{Machine, Inspection};` |
| Registered external type import | `import type gal.time::Instant;` |
| Entity | `entity Machine { key id: Id; field status: Status; owns Serial [0..1]; }` |
| Relation | `relation Inspected { role asset: Machine [1..1]; role report: Inspection [1..*]; }` |
| Attribute domain | `attribute Serial: String;` |
| Type alias | `type InspectionHistory = List<Ref<industry.assets.Inspection>>;` |
| Structured type | `struct AssetSummary { field status: Status; }` |
| Enumeration | `enum Status { Active, Retired, }` |
| Invariant | `invariant SerialPresent on Machine { serial != "" }` |
| Derivation | `derive IsRetired on Machine.retired: bool = status == Status::Retired;` |
| View | `view AssetStatus from Machine { project status; where retired == false; }` |

Imports may select a single symbol with `::Name` or a list with
`::{Name, Other}`. The optional quoted version follows the selection.
Unpinned syntax represents an import to be resolved through a later lock.
The type modifier marks an external registry lookup. A trailing `as Alias`
after the optional version aliases the namespace or a single selected symbol.
The analyzer rejects an alias on a group of multiple symbols. Enum payloads
are deferred. The AST gives each variant its own range.

`Option<T>` and `List<T>` can nest. `Ref<T>` takes a named target.
A named type may denote a local definition, a cross-module type or a
registered external type. Numeric width and cardinality checks, reference
kinds and collection bounds belong to later semantic analysis.

### Expressions

From highest to lowest precedence:

| Form | Associativity |
| --- | --- |
| Property access `.` | Left to right |
| Prefix `!`, `-` | Right to left |
| `*`, `/`, `%` | Left to right |
| `+`, `-` | Left to right |
| `<`, `<=`, `>`, `>=` | One comparison per level |
| `==`, `!=` | One equality operator per level |
| `&&` | Left to right |
| `||` | Left to right |

Parentheses preserve an explicit group. Terminal expressions are literals or
symbol paths. Same-precedence infix operators, prefixes and property chains
are stored as sequences to bound the depth of long, flat expressions.
Invariants and derivations spell their target with `on`. Views specify one
source with `from`, one or more `project` statements with an optional
`as` name, then an optional `where`. These forms and operator rules
are Phase 2 decisions for the areas left open by v0.3.

### Actions and security

An action declares named, typed parameters, an optional result and
`requires`, `ensures`, `effects` or `retry` clauses. The semicolon
ends the contract. Effects have a named target and use `read`, `write`
or `emit`. Retry declarations use `idempotent`, `non_idempotent` or
`deduplicated(parameter)`. Clause multiplicity and validity remain semantic
checks.

An `access Target` block can name an external resource, define local
permissions and map fields with `field name requires Permission;`.
Permission bodies use `relation: "…";` for SpiceDB and `cedar: "…";`
for a Cedar action reference. The latter is a quoted external name. Policies
use `permit Target` or `forbid Target`, followed by optional
`when { expression }` and `unless { expression }` in that order, then
`;`. Targets are explicit GOL paths. The expressions are GOL syntax.
Native Cedar bodies and external schema and bundle linking are deferred.

`security label Name { … }` accepts a confidentiality `classification`,
optional `integrity` and lists of `compartments`, `destinations`
and `purposes`. Fields can attach `@label(Name)` and
`@requires(Permission)`. The protection annotation and the path-based policy
scope are Phase 2 spelling choices. The AST carries every written binding and
label declaration for later validation. Policy decisions, IFC propagation and
runtime effects belong to later components.

The [mail fixture](../../../platform/ontology/rust/tests/fixtures/mail.gol)
combines these forms. Its `Message` refers forward to its `Attachment`
and status types, imports a temporal type and binds a permission to SpiceDB
and Cedar. A scoped policy supplies a GOL condition and the action describes
its contract.

## Parser API and diagnostics

The default Cargo feature `parser` exposes
`galadril_ontology::syntax::parse_source(name, text)`. The caller reads or
owns the UTF-8 source. The AST borrows names, numeric lexemes and unescaped
string text. Only escaped literals are decoded into owned strings. Nodes
carry half-open byte ranges relative to their `SyntaxTree::source`.
`parse_sources` yields one result per input in input order, on demand.
Compilation can use those trees in a later symbol collection and resolution
pass. Import paths and external references remain data in the AST.

Malformed input returns the first detected `ParseError` with a stable
`gol::` code, source name, byte range and an expected construct where
available. Attach the original text using `with_source` for miette
presentation. The library traces only the rejection code through the host.
A lexical pass bounds each source at 1 MiB, 32 nested delimiters including
generic brackets, and 32,768 lexical units. The work budget counts words
or digit runs, literals and individual punctuation bytes. Whitespace and
comments contribute to the byte budget. These are parser limits independent
of future compiler or execution budgets.
