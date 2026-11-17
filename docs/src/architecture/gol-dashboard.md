# GOL dashboard and public API

The ontology workspace uses GOL source files as its only authoring model. Registry
owns parsing, semantic validation, security compilation and publication. The
browser does not deserialize a parallel semantic document or parse YAML.

## Authoring and disclosure

Opening an existing ontology first requests the authorized catalog and a compiled
symbol projection. Full source is requested only by the explicit **Edit GOL**
action. Its server admission requires the complete source-authoring authority and
IFC floor; an edit capability hint grants no source access. A source file's `path`
is a logical package name, never an instruction to open a filesystem path.

The source editor supports multiple files, immutable source revisions, precise
compiler diagnostics and compare-and-swap saves. Invalid syntax can be saved for
correction. Validation uses the saved source revision; any subsequent edit
invalidates the publication review. Publishing reviews the exact validated source
package and activates a complete generation through Registry. Draft creation and
publication remain separate operations.

Compiler diagnostic spans are UTF-8 byte ranges. The browser maps them to complete
Unicode characters and UTF-16 textarea selections without reinterpreting GOL.

## Runtime exploration

`golOntologySymbols` returns `{generation, symbols}`. Every symbol, property name,
property type and reference is authorized by the server. The graph draws edges
only between endpoints present in that projection. JSON Schema previews use its
exact generation and an explicit selection of authorized properties. The standard,
OpenAI strict and Outlines profiles reuse the shared projection engine.

No browser cache grants access. Session changes, access invalidation and KeepAlive
deactivation cancel requests and discard source, diagnostics, names and schemas.
Request epochs prevent cancelled responses from restoring stale data. Browser
error messages exclude dependency details and protected metadata.

## API contract

The typed GraphQL queries are `golOntologies`, `golOntologyCanManage`,
`golOntologyWorkspace`, `golOntologySymbols` and `golOntologySchema`. The source
lifecycle mutations are `saveGolOntology`, `validateGolOntology`,
`publishGolOntology` and `deleteGolOntology`. `bindGolOntology` captures a published
generation in a pipeline draft; `ontologyBindings` returns GOL symbol selections
with generation and source provenance.

A workspace contains `summary`, source `files`, `diagnostics` and resolved
`dependencies`. A summary separates `sourceRevision` from `activeGeneration` and
`activeSourceRevision`. Validation returns `valid`, `invalid` or
`pending_authority`; correct syntax alone never proves security approval.

## Verification

`//front/dashboard:ontology_contract_test` checks transport validation, source
preservation, generation pins and Unicode diagnostic positions.
`//front/dashboard:ontology_browser_test` uses explicit API fixtures to check UI
behavior, access cleanup, accessibility and desktop/mobile layouts. Those fixtures
are UI contract coverage, not interservice evidence.

`//tests/e2e:gol_lifecycle_test` uses the shipped dashboard, real Gateway, Registry,
RustFS, lakeFS, Ory and SpiceDB. It exercises invalid imports, source publication,
generation pinning, authorized JSON Schema, live permission revocation and
deletion. It never intercepts GraphQL. Docker and deployment security configuration
are required; a skipped or unavailable environment is not a passing E2E result.

The source-first editor expresses entities, relations, imports, security contracts,
actions and other supported GOL syntax. Specialized visual construct editors are
not additional ontology definitions. Structured JSON remains subject to GOL
invariant, reference and authorization checks before ingestion.

The real-service browser profile is explicitly separate from the production
location policy. Its authority bundle includes the Gateway clock and verified MFA
facts, and does not declare a location fact: the existing Ory browser flow does
not issue a trusted `region` claim. Registry and Gateway use the same test bundle
and its own authority fingerprint. This test does not establish geolocation
behavior. The production profile continues to refuse metadata operations when a
required signed location is absent; missing-context checks remain covered by the
shared security-engine tests.

The browser transport currently supplies `mfa_verified = false` conservatively.
It does not elevate an assurance claim into a verified MFA fact. Policies that
require MFA therefore remain unavailable through this transport, including when
MFA is optional for the tenant. Extending that support requires an explicit
revocable credential-generation check; the existing session check only requires
that proof when tenant MFA is mandatory. Background work cannot invent or reuse
an MFA fact from caller JSON.

Graph results preserve a `GraphGolReference` containing `tenantId`, `ontologyId`,
`ontologyVersion`, `sourceRevision`, `generationId`, `symbolKey` and `symbolKind`.
Entity, event and latent-identity nodes reference GOL entities; state and causal
variable nodes reference an explicit `module::Entity.field`. These tuples are
checked lexically at the graph boundary and still require live metadata admission.
A source revision, a module version and a compiled generation are separate pins.

New source drafts include explicit `access Example` contracts using local
`read_access` and `write_access` names bound to the deployment's external `read`
and `write` permissions. Bare `read` and `write` are reserved effect keywords in
GOL and cannot name local permissions. The starter also declares describe,
transfer and execute bindings. Registry still checks the complete approved
profile; these authored declarations grant no relationship or user access.

Catalog listings and their local search are bounded to 100 authorized summaries;
there is no invented pagination cursor. A known ontology URL obtains its symbol
projection directly when its summary falls outside that catalog window. A fallback
name and generation are displayed only after a nonempty authorized projection.
This path never loads raw source for a reader.

A failed source save retains both the local draft and its original comparison
revision. Refreshing runtime metadata does not rebase the draft. Authors may
explicitly review the latest protected workspace beside their local sources,
then keep their draft against that reviewed revision. This action only selects a
new comparison baseline: a later save still uses compare-and-swap, and validation
must run again before publication. No ontology branch or merge engine is created.
