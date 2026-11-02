# Ontology and pipeline workspace verification

## Scope

The ontology workspace opens the complete model before drilling into a type.
It shares the pipeline graph palette, provides an explicit edit mode, guided
object creation, editable relationships, contextual YAML and property-type help.
Selecting an active type again returns to the complete model. Contact mappings
remain an optional, collapsed setting of the complete model. Pipeline binding
controls belong to the pipeline workflow.

Pipeline review uses the editor's node cards and exposes before/after values and
YAML comparison. Branch deletion rejects main, dependent branches and pending
proposals. Mail references open authorized messages and collected threads; the
read flow has no send operation.

## Completed validation

All builds and Bazel tests used BuildBuddy remote execution with local fallback
and local action disk cache disabled.

- `//front/dashboard:pipeline_workspace_browser_test`: 24 scenarios passed,
  including graph connector dragging, relationship editing, guided creation,
  YAML validation, mail references, permission changes and narrow layouts.
- Ontology contracts, Pengolo Markdown, dashboard type, tools, lint and format
  targets passed.
- Registry domain, Registry library and semantic targets passed. Gateway tests
  and Vision execution tests passed.
- Workspace Clippy passed with all targets and features. Ruff format checked 531
  files; Ruff lint passed. Changed Rust files passed nightly rustfmt.
- The Studio ARM64 image and Gateway ARM64 binary built remotely. Local service
  updates preserve the existing environment and do not restart the proxy.

The required `bazel test //...` gate was attempted. It failed before the shared
UI browser test because a Linux `coreutils` binary was executed on macOS. It is
not a passing workspace gate. Workspace Cargo formatting also reports existing
differences outside the changed Rust files. The Python manifest command hit the
same executable-format boundary; Gazelle initially encountered disk exhaustion.
Only the explicitly approved temporary Cargo artifacts were removed.

## Bounded code-quality review

Crapper measured complexity without compatible function-level coverage. Coverage
and CRAP are **unavailable**, rather than zero or passing scores.

| Changed function | CC before | CC after |
| --- | ---: | ---: |
| `Registry::author_pipeline` | 38 | 39 |
| `AuthoringCommand::operation_name` | 9 | 10 |
| `ontologyPrimaryAction` | new | 4 |
| `sameOntologyDefinition` | new | 3 |
| `canonicalDefinition` | new | 4 |
| `withOntologyLink` | new | 7 |
| `withUpdatedOntologyLink` | new | 8 |
| `withoutOntologyLink` | new | 6 |
| `withOntologyType` | new | 12 |
| `ontologyCodeLines` | new | 10 |

The object-type function retains one explicit validation guard for bounded,
unique author input. YAML range traversal remains cohesive with its selected
resource calculation. The existing Registry command dispatcher retains its
atomic commit boundary. These are reviewed complexity exceptions, not CRAP
results. Vue component coverage was not measured by this pilot.

Dryer found no candidates at similarity 0.8 within the five selected TypeScript
and Rust files. Six selected mutations of publication identity, lifecycle and
relationship endpoints were killed by completed Bazel tests. Per-mutant logs
were checked for actual assertion/domain failures; build failures do not count.
This is a bounded mutation pilot, not whole-project mutation coverage.

## Runtime limits

A tenant copy of the email ontology was published and bound to the mail decision
block in a new draft. Historical executions and published pipeline revisions
were not rewritten or replayed. No diagnostic email was sent.

The scanned-PDF indexing incident remains unresolved: the baseline OCR exceeded
the parse deadline, and a CPU float32 candidate exited with status 137. The
candidate was reverted. This work does not claim successful indexing or a
confirmed out-of-memory diagnosis from that exit status alone.
