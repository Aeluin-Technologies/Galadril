# Pipeline Builder

## Findings and implementation

| Finding | Technical and interaction cause | Solution | Implementation |
| --- | --- | --- | --- |
| Incomplete drafts cannot be saved | Both the editor and Gateway run execution validation before persistence | Separate bounded authoring documents from executable compositions | Explicit authoring commands; approval and deployment retain authoritative validation |
| Layout disappears after a reload | Node positions exist only in Vue Flow | Persist layout with the immutable definition | Versioned `editor.positions`; export preserves authored layout |
| Settings obscure the canvas | Block settings wrap an already nested sidebar in a modal | Keep selection and settings beside the graph | Contextual inspector with progressive advanced controls |
| Commands compete with the pipeline title | Graph tools and revision transitions share a single toolbar | Separate identity, collaboration navigation and graph editing | Persistent title; primary draft save; revision action menu; Editor, Proposals & branches and History tabs |
| Blocks are hard to distinguish and can overlap | Uniform cards hide their role; count-based placement reuses deleted positions | Use a documented accessible categorical palette and deterministic placement | Teal sources, violet processing, ochre control, blue outputs; explicit topological arrangement; persisted manual positions |
| Errors do not identify affected blocks | Exceptions collapse to a single invalid flag | Return bounded issue codes and block identities | Local graph diagnostics and explicit, previewable repair proposals |
| Plugin and model selection are conflated | Raw implementation paths are used as ordinary settings | Separate model aliases from registered plugin operations | Plugin then capability selection; catalog-driven fields |
| Conditions expose payload paths | Existing routing configuration mirrors runtime internals | Use business fields, typed operators and values | Shared bounded condition contract for routing and filtering |
| Multiple inputs have ambiguous semantics | Runtime intentionally accepts one streaming predecessor | Add explicit event union, retain rejection for implicit stateful joins | Forward step with `merge=union`; one output per incoming event |
| Pipeline collaboration is missing | Registry implements ontology merging only | Add persisted branches and reviewed proposals with compare-and-swap | Three-way merge by block identity; path-level conflicts and explicit choices |
| Publish also activates runtime discovery | One pointer carries authoring and runtime state | Separate approved content, release and active runtime revision | Approval bound to definition hash; release pin; explicit deployment |
| Email automation lives in personal settings | The reply worker consumes an account profile and pinned binding | Configure automation in the pipeline while preserving mailbox authorization | Pipeline email configuration reuses profile validation and the existing send boundary |

## Compatibility and security

Existing strict creation, update, composition, publication and pinned reads remain
available to existing callers. The new authoring operation is a separate contract.
Gateway checks current tenant membership and SpiceDB/Cedar permissions on every
operation. Registry commits the complete workspace atomically to lakeFS. Expected
heads protect branch edits, proposals and lifecycle transitions from stale writes.

Draft storage accepts disconnected or incomplete graphs, but never deployment
credentials or service configuration. Graphs are bounded to 256 blocks. Branches,
proposals, configuration size and merge recursion are bounded. Approvals are tied
to content and invalidated by edits and merges. Publication does not make a new
revision visible to runtime discovery. Deployment revalidates pinned references and
current publication rights before moving the existing runtime pointer.

An event union does not synchronize or correlate predecessor results. Each
incoming event continues independently. A stateful join remains a separate future
runtime capability and must not be represented as a working union.

The collaboration operations are implemented here, rather than inferred from
Registry's ontology support. Each source branch has at most one pending proposal
into main. The proposal pins both participating content hashes, and a changed
participant requires a refreshed proposal. History comparisons and restoration
use immutable revisions; restoration creates another draft and does not move the
runtime pointer. Multi-stage review policies and arbitrary branch-to-branch merge
targets are outside this implementation.

Email templates are bounded variable substitution, with no expression evaluation.
The compose capability creates or reuses a durable source-bound draft. Sending
uses that draft through the existing mailbox policy and approval boundary.
Mailbox credentials stay in the existing secret store. The pipeline holds the
generation configuration and the mailbox profile holds its exact deployed pin.

## Verification

Unit and contract tests cover incomplete drafts, immutable release/deployment
separation, optimistic concurrency, keyed three-way merges, localized diagnostics,
business conditions, explicit unions, bounded templates and source-bound drafts.
Registry's gRPC integration exercises real lakeFS and S3-compatible storage.
Browser scenarios cover the authoring lifecycle, proposals, conflicts, pinned
links, plugin selection, stale versions and the email flow, including desktop,
narrow-screen and accessibility checks.

The live Gateway scenario in `tests/e2e/pipeline_authoring.py` also exercises real
IAM and immutable storage as part of the complete lifecycle suite. Its addition
does not imply that the full suite has passed: the local full E2E build exhausted
disk while fetching GPU dependencies. The workspace-wide Bazel run separately
stopped on a Linux coreutils executable selected on macOS. Linux CI remains the
required confirmation for those checks and for coverage.

## References

- [Foundry navigation](https://www.palantir.com/docs/foundry/pipeline-builder/navigation/)
- [Fabric activities](https://learn.microsoft.com/en-us/fabric/data-factory/activity-overview)
