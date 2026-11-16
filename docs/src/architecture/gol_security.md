# GOL security contracts

`galadril_ontology::authorization::compile` accepts a checked GOL
`Project` and a trusted `SecurityEnvironment`. It indexes security
descriptors by `Route`, the pair of a version-pinned logical target and
an operation. The resulting `Contract` holds the bindings, linked Cedar
schema and policy sets, effective flow labels, projected Cedar inputs,
prerequisite routes and static pins. These are the inputs to the future
security index for `.golc`.

An invalid compilation returns target-associated diagnostics. Missing
authority inputs, unresolved Cedar declarations, missing routes or adapters,
and emitted effects without a checked transfer contract remain
`PendingExternalValidation`. Only a `Validated` result yields a
contract through `Compilation::publishable()`. This is eligibility under
the static profile. Registry still owns publication, and a runtime still has
to check each access.

## Static authorities and bindings

The host supplies approved, immutable revisions of the route configuration,
SpiceDB schema and ID bridge, and Cedar policy bundle. An `AuthorityRevision`
pins each version to the SHA-256 digest of its approved artifact or manifest.
The authority verifies the artifact before passing its metadata to GOL. The
runtime compares both versions and fingerprints with the compiled pins.
A relationship snapshot links each exact GOL resource type to an external
object type and the permissions defined on it. The compiler verifies the
shape and membership of each referenced permission. Authenticated subjects
and dynamic relationship assignments remain with IAM and SpiceDB.

The object bridge encodes four delimited components as hexadecimal bytes.
The components are tenant, ontology identity, local declaration name and
logical instance ID. The instance's schema version is checked separately,
which keeps compatible revisions on the same relationship object. An empty
component or an encoding longer than 1,024 bytes prevents a permission check.
Tuple writers and readers must use the same approved bridge revision.
[SpiceDB identifier rules](https://authzed.com/docs/spicedb/concepts/zanzibar/#identifiers)

The host approves each Cedar resource projection and applicable action UID.
The compiler generates Cedar resource and action definitions with required
resource attributes, context types and the external principal declaration.
Supported projected values are `bool`, `String` and `i64`,
including transparent aliases. IAM fragments provide concrete principal
definitions. The official SDK performs complete schema linking and strict
policy validation. An invalid action application also blocks publication
when Cedar reports it as a warning. A declared binding must match its
generated action, principal and resource profile.
[Cedar schema linking](https://docs.cedarpolicy.com/schema/schema-linking.html)

GOL `when` and `unless` predicates lower to Cedar for direct
projected fields, typed context keys, scalar literals, Boolean operations and
comparisons supported by both type systems. Unrepresentable types, arithmetic
and paths outside the approved projection produce diagnostics. A 256-node
lowering budget bounds generated policy expressions. The native policy bundle
and each applicable GOL policy layer must independently allow the request.
Within a layer, Cedar combines its permits and forbids. A forbid-only GOL
layer restricts the native grant. It has an internal baseline permit that
cannot satisfy the native bundle.

## Route composition and IFC

Phase 7 adds explicit `Describe` and `DescribeTransfer` routes for schema
metadata. A read route does not imply them. The metadata exporter checks each
selected field and named type, preserving the same binding, Cedar, dependency
and IFC obligations. `DescribeTransfer` requires Describe and distinct external
transfer bindings. Enums, type aliases and scalar attributes can now carry
resource-level access contracts. See [authorized JSON contracts](gol_export.md)
for projection rules and the conservative nested-write boundary.

- Deployment configuration assigns one or more named permissions to each
  exposed operation. A field inherits its owner's permissions for the same
  operation and adds its declared guards. Generic and permission-scoped Cedar
  conditions are each retained. A missing mandatory mapping blocks static
  publication, and an unconfigured runtime route is denied.
- Authorizing a resource symbol covers that route's resource-level operation.
  The host checks the field routes for the fields it releases or modifies.
  Whole structured values are rejected as field releases until child-level
  enforcement can be represented. Entity references may cross the boundary
  as opaque identities under their field route. Loading a referenced
  resource requires its own instance and authorization.
- A transfer requires the read permissions and a distinct SpiceDB permission
  and Cedar action. Transfers of derived values also depend on read and
  transfer checks for the statically known source fields and control inputs.
  An external sink always requires a transfer route.
- The host supplies an explicit IFC floor, optionally augmented per ontology.
  Entity, field and runtime instance restrictions can strengthen that floor.
  The confidentiality join takes the higher of Public, Internal, Confidential
  and Restricted and unions compartments. Purpose and destination allowlists
  intersect. An omitted list adds no restriction, and an explicit empty list
  denies all. Every release also needs an approved sink, its clearance and
  compartments, and an approved purpose.
- The compiler propagates restrictions through derived fields and views.
  A view inherits its projections and filter inputs, including filters that
  only affect whether a result appears. Prerequisite routes are transitive
  and require evidence for concrete, separately authorized instances. The
  worklist also accounts for declared action inputs and writes. Integrity
  labels require a future integrity policy. Explicit declassification is
  outside this profile.

## Runtime boundary and freshness

`authorize` is the decision entry point. The trusted runtime authenticates
the IAM principal and supplies the exact instance revisions, approved
destination and purpose, active static contract pins, typed context and
complete instance evidence for the route's prerequisites. An access intent
contains no trusted authentication or contextual claims. The runtime binds
its evidence to the actual request and must use issuer- and tenant-qualified
subject identities. The gate checks tenant equality for every instance and
uses only the requested target's indexed obligations.

The `PermissionProvider` checks the exact subject, resource and permission
with either fully consistent reads or a required minimum ZedToken. A grant
must attest to the requested consistency. Opaque ZedTokens are not ordered
locally. A denial, unresolved caveat, unavailable provider or insufficient
freshness denies the request. Every decision performs current checks.
[SpiceDB consistency](https://authzed.com/docs/spicedb/concepts/consistency)

Cedar validates the request and loaded entities against the linked schema.
The gate inspects the diagnostics for each required result. An evaluation
error denies the request even if Cedar also returned an allow from another
policy. The gate conjoins runtime validity, relationship checks, all Cedar
layers, and IFC sink checks. OTLP-compatible rejection events contain only
the bounded failure category.

The production adapters still need to connect Gateway, IAM, SpiceDB and the
trusted entity loader to this interface. They must bind the same tenant and
runtime snapshot across all proofs, enforce revisions at use, and retain the
instance IFC restrictions when a value is stored, cached or passed to another
component. Exports, logs, caches, derived results and action effects need
their own mandatory release hooks. This phase implements a static compiler
and an embeddable gate, and its tests use deterministic authorities. It
provides neither cross-system atomicity nor general non-interference.

The Cargo feature `authorization` enables the gate without the GOL parser
when default features are disabled. Compiling a `Project` into the
contract additionally needs the `parser` feature. The static contract is
kept in memory by the security compiler. For an authenticated local artifact,
see [GOLC Local Archives](gol_archive.md).
