# Plugin ontologies and tenant overlays

Plugins ship semantic definitions, not tenant business identities. The Utils
`utils.mail` package contains five linked modules: people and addresses, message
content and signatures, attachments, threads, and messages. Sender, recipient,
copy and blind-copy relationships initially target the package's own person type.
No tenant type is selected by name or imported automatically.

## Authoring and execution

Open **Ontologies** from the pipeline catalog, or **Email data model** from email
settings. The workspace separates plugin originals from organization ontologies.
It provides type search, property tables and a relationship graph using Galadril's
amber controls. Users can inspect originals; tenant administrators can create
their own types and properties, review a plugin overlay, and save a tenant copy.

To connect recipients to an existing business type, select a published tenant
ontology and its object type for the recipient relationship. Other roles remain
isolated unless separately selected. Preview and save both recompute the result
on the server from the trusted package, exact package version and authorized,
immutable target revision. Only the selected types and Registry's dependency
closure are imported. Unknown slots, non-object targets, collisions, dangling
references and invalid semantic graphs are rejected without changing storage.

Saving, publishing and binding are separate actions:

1. Save a draft with compare-and-swap against the captured tenant head. A stale
   head fails instead of overwriting another author's work.
2. Publish the reviewed ontology through Registry's existing production lifecycle.
3. Select a pipeline and an executable block. One Registry authoring command saves
   the new `ontology_required` draft and its semantic selector atomically. Review,
   publish and deploy that draft through the pipeline lifecycle.

Binding never redeploys a pipeline. Registry publication rejects a required block
without a binding. A binding rejection, stale revision or failed storage commit
leaves both the existing definition and its bindings unchanged. Older Registry
servers reject the new command instead of silently applying half the operation.
Deployed evidence keeps its original tenant/ontology/revision/resource identity.

## Ownership and authorization

- Registry retains validation, snapshots, bindings and publication authority.
- Gateway verifies membership for catalogs and resource visibility for pinned
  reads. Creation requires `create_ontology` on the tenant, editing requires
  `edit` on the ontology, and publication requires `publish`. Binding requires
  pipeline editing and ontology visibility. Every decision uses SpiceDB and Cedar.
- The email shortcut starts a new tenant copy and appears only after its
  `create_ontology` check succeeds. Failed or unavailable checks keep it hidden;
  the same decision is independently enforced by Gateway writes.
- Save and binding audit events remain distinct from publication events.
- Overlay provenance records plugin/module versions, explicit participant mappings
  and the selected target revision. Original package files are never modified.
- A tenant-local copy does not change another tenant's same-named ontology.
- Bcc's `mailbox_owner` annotation documents intended visibility; it is not an
  authorization policy. Existing mailbox and sending policy checks still apply.

## Operational mail data

This feature provides editable semantic definitions and runtime binding. It does
not automatically extract people, recognize signatures, resolve real identities,
or materialize all historical mail as ESKG instances. Such extraction must be an
explicit pipeline operation with its own provenance and authorization checks.

The email settings view continues to use the mailbox's operational store for
pagination, drafts, send state and IMAP/SMTP idempotency. The direct data-model
link opens the same ontology workspace; there is no second ontology editor.
Replacing that store with graph-backed message instances would require a separate
migration, not merely a different frontend query.

## Quality evidence

All Bazel tests and coverage actions use BuildBuddy remote execution, with local
fallback disabled for mutation runs. Quality tools use an isolated source snapshot
and existing private tool installation, without adding project dependencies.

| Changed function | Before CC / coverage / CRAP | After CC / coverage / CRAP |
| --- | --- | --- |
| Python package validation `bounded` | 11 / 72.2% / 13.6 | 4 / 100% / 4.0 |
| `_module_resource_ids` | Part of `bounded` | 6 / 100% / 6.0 |
| `_package_resource_ids` | Part of `bounded` | 4 / 100% / 4.0 |
| Rust `materialize` | 40 / unavailable / unavailable | 10 / 97.1% / about 10.0 |
| `package_resources` | Part of `materialize` | 9 / 100% / 9.0 |
| `target_resources` | Part of `materialize` | 6 / 100% / 6.0 |
| `rebind_participant` | Part of `materialize` | 6 / 100% / 6.0 |
| `resource_id` | Part of `materialize` | 1 / 100% / 1.0 |
| Atomic `bind_ontology_draft` | New | 5 / 100% / 5.0 |
| Authoring command dispatcher | 36 / unavailable / unavailable | 38 / 100% / 38.0 |

Coverage comes from the scoped Bazel LCOV reports for `ontologies_test` and
`registry_test --test_arg=plugin_ontology::tests`, mapped to the tested sources.
The retained `materialize` score slightly exceeds 10 before rounding: it
coordinates validation and ownership boundaries, including fallible Rust `?`
operations. Keeping those steps together preserves the semantic transaction.
The existing authoring dispatcher remains above 30: it handles the bounded
Save/Branch/Proposal/Merge/Approve/Publish/Deploy state machine and now delegates
binding to the small helper. Its measured complexity increased from 36 to 38;
the domain suite reports 100% LCOV coverage. This is a retained review exception,
not a claim that line coverage proves every state-machine path.

Mutation tests initially found five surviving Python mutations. Exact byte and
resource boundaries plus malformed-resource cases killed all nine initial sites.
After separating validation responsibilities, all eight available Python sites
are killed. The three available Rust sites initially had two survivors: missing
owned properties and incorrect reference replacement. Tests now assert both, and
all three mutations are killed. These are completed failing tests, not build or
infrastructure errors counted as kills. The mutator does not instrument every
Rust macro guard; the percentages apply only to the selected sites.

The new atomic binding helper has three additional targeted mutation sites.
One initially survived because the fixture did not distinguish dependency
closure from selection of all structural types. A referenced function outside
that selector now proves the closure is retained; all three sites are killed.
Seven older dispatcher sites were excluded by the explicit line filter; the
tool's `UNCOVERED` label for these unselected sites is not a coverage result.
The PostgreSQL rollback scenario uses a real deferred constraint-trigger failure;
SQL statements themselves are outside this mutator's operator set.

Frontend parser, browser, accessibility and responsive tests exercise the user
flows. Function-level frontend coverage and CRAP remain unavailable: browser
success alone is not a coverage measurement. The tool aggregates nested Vue
callbacks under their composable, which must be considered when reading its
complexity output. Scoped Dryer analysis at similarity 0.8 found no duplication
candidates in the new ontology validation, materialization and frontend modules;
this is not a monorepo-wide duplication claim.

Tool revisions: Crapper `6fd92c4c9bcc56efb64a2f8743d5a2993bcfd42d`, Mutator
`a5a0cb803b66e0cdb8bf471b3b08af701d424a41`, Dryer
`66ff6d21a42c04afcad89c78a80066176d1294b0`. Mutations use one worker, existing
coverage and a BuildBuddy-only test adapter, with infrastructure failures reviewed
separately.

Targeted BuildBuddy checks passed for Registry, Gateway, the packaged plugin
bundle, Utils, Vision execution, ontology parsing, pipeline/ontology browser
scenarios, accessibility, localization, UI type checking, lint and formatting.
Clippy passed with all targets/features and warnings denied; changed Python
files passed Ruff. Changed Rust files passed nightly rustfmt checking.

The whole-workspace Bazel gate and Gazelle runners remain blocked by Linux host
tool binaries being invoked on macOS (`coreutils`/runner `Exec format error`).
Whole-workspace `cargo fmt --check` also reports formatting in untouched files.
These checks are not reported as passing. The full lifecycle E2E run is tracked
separately; targeted test success does not establish a passing full lifecycle.
