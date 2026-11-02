# Pipeline and ontology workspaces

The application shell owns the viewport. Workspaces constrain their flex/grid
children with `min-height: 0` and scroll inside their content panels; the document
body does not scroll beyond the application. Settings retain a scrollable content
panel, while pipeline graphs and inspectors fill the available editor space.
Primary navigation, selections and actions use the amber semantic tokens.

## Authoring and review

A pipeline edit immediately updates diagnostics on its affected blocks. Local
structural errors prevent an unnecessary server composition request. After 1.2
seconds without edits, valid local graphs are checked by Gateway. Registry and
the runtime catalog remain authoritative. The toolbar presents a compact status;
additional diagnostics and proposed repairs remain available on demand. Repairs
still require an explicit preview and approval.

The editor enables saving after successful verification. The underlying draft API
continues to accept incomplete drafts; this UI policy does not redefine Registry's
draft storage contract. Review, publication and deployment remain distinct steps.
A verification failure exposes a retry action rather than a page-wide error banner.
Reloading or applying a rebase schedules verification even if the resulting content
matches the previous local draft.

Branches are independent drafts. New branches record their actual `parent_branch`
in Registry; the UI does not infer ancestry from a common merge base. Older
branches without this metadata display "Origin not recorded". The tree can be
collapsed, and creating a branch checks it out for editing. Pipeline listings load
branch details only when expanded.

Proposals and revisions have dedicated routes. Proposal reviews display the
resulting graph, per-block comparisons and explicit conflict choices. Merging
requires confirmation and the expected head revision; it updates the main draft
without publishing or deploying it. Revision comparisons are read-only. Restoration
creates a new draft after confirmation, and a branch absent at the selected
revision is rejected rather than substituted with main.

The review workflow follows the separation of editing, proposals and history in
[Palantir's navigation](https://www.palantir.com/docs/foundry/pipeline-builder/navigation/)
and the per-change review progression in
[GitHub's review workflow](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/reviewing-proposed-changes-in-a-pull-request).
Exact configuration remains available under advanced details.

## Ontology access and navigation

`/ontology` lists authorized organization ontologies and isolated plugin originals.
`/ontology/new` prepares a tenant draft, optionally from a selected package.
`/ontology/edit/[id]` opens an existing tenant ontology. The editor's return link
points to all ontologies. The email shortcut is labelled "Email ontology" and
retains its ontology-creation permission check.

Global search combines its static ontology shortcut with a separate authorized
ontology catalog request. Dynamic results have bounded canonical internal paths,
are cancelled when superseded, and are cleared on account, tenant or access
invalidation. An unavailable graph search does not erase ontology results.
An unavailable plugin catalog or unrelated pipeline permission does not prevent
opening an authorized custom ontology. These presentation changes introduce no
new execution or ontology management permissions.

Gateway's Compose default now matches its application default: Scribe is enabled
unless explicitly disabled. `/plugin-ontologies` is routed through Gateway's egress
proxy and allowed only for Gateway's authenticated GET catalog policy, including
the Kubernetes equivalent. POST remains denied. Apply the updated configuration
and proxy policies to running deployments; editing source does not update existing
containers.

## Verification scope

BuildBuddy remote execution passed the affected Registry domain/unit tests,
Compose and Envoy contracts, and the real Envoy proxy integration test. The
production frontend target `//front/dashboard:pipeline_workspace_browser_test`
passed 13 Playwright scenarios covering review, branching, publication/deployment
separation, incomplete blocks, ontology isolation, read-only access, search,
revocation, mobile layouts and viewport containment. Frontend type checks, tools,
lint, format, localization and targeted unit tests also passed. Rust Clippy and
changed-file nightly rustfmt checks passed; changed Python tests pass Ruff.

Quality measurements use an isolated snapshot and Node-generated LCOV collected
by Bazel on BuildBuddy. No project dependencies were installed for this analysis.

| Changed function | CC | Function branch coverage | CRAP | Interpretation |
| --- | ---: | ---: | ---: | --- |
| `searchOntologies` | 4 | 100% | 4.0 | Bounded catalog matching and stable navigation identities. |
| `reviewChanges` | 19 | 87.0% | 19.8 | Retained cohesive comparison across sources, steps and settings, including optional editor labels. Above 10 is accepted here; it does not justify fragmenting the identity-matching algorithm. |

Mutator executed six selected search mutants and eight selected comparison
mutants: **14 killed, zero survivors**. The initial search run exposed missing
empty-query, length-boundary and distinct-result-identity tests; those tests were
added before repeating the run. Invocation logs confirm completed test failures,
with no build failures counted as kills. This is a bounded mutation pilot, not
complete mutation coverage of the files or application. Existing wire parsers in
these files are outside this pilot; their unexercised branches in this particular
coverage run do not establish their whole-suite coverage.

Dryer found one identical five-line bounded-string guard across the ontology and
pipeline-review wire parsers. Each guard retains its own domain error contract;
no shared domain decision was extracted from this small primitive coincidence.

The required whole-workspace `bazel test //...` is still blocked on macOS by a
Linux coreutils staging action (exit 126). Gazelle and its Python manifest runner
likewise build remotely but cannot execute their Linux host binaries on macOS.
Whole-workspace `cargo fmt --check` reports pre-existing formatting differences
outside the changed Registry files. These checks are not reported as passing.
The earlier complete pipeline lifecycle E2E run predates this UI change; it is
not a new execution proof for a live mailbox or the currently running deployment.

## Rollout compatibility

Existing branch documents remain readable without `parent_branch`. New branch
writes add this optional field. Older Registry binaries using strict unknown-field
rejection cannot read that new metadata; coordinate the Registry rollout before
creating new branches. Do not rewrite immutable history to facilitate a downgrade.
