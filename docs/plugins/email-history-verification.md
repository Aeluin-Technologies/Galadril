# Email history verification

Reviewed on 2026-11-02 against base `6e60aff4`, in an isolated worktree.

The implementation keeps chat tools read/draft-only. They cannot approve or
send, even for a fully privileged user with an already approved draft. Automatic
delivery requires the deployed pipeline policy, current mailbox rights and the
existing delivery claim. A required Scribe decision is persisted and checked
again at the output boundary.

## Behavioral evidence

- Eighteen targeted Bazel suites passed through the existing BuildBuddy setup,
  including utils API/security/transport/workflow/pipeline/lifecycle tests,
  Gateway bridge/activity contracts, Scribe drafting and frontend checks.
- The final history suite contains twenty cases for source binding, historical
  collection, attachment reads, bounded context, decision replay, human approval,
  authorization and long-message pagination.
- Eight Python suites passed under Bazel coverage instrumentation. Instrumentation
  was enabled temporarily and the original toolchain configuration restored.
- Two production-browser scenarios passed: a source-bound reply and a standalone
  proposal with sender selection. They cover keyboard expansion, attachment
  text, wide/narrow layouts, approval and one-shot sending. Provider/Gateway
  responses are fixtures. The separate utils end-to-end test uses real TLS IMAP
  and SMTP protocol servers; this is not live-provider or deployment validation.
- Workspace Ruff format/check and Cargo Clippy checks passed. Changed Rust files
  pass nightly rustfmt. Workspace formatting still reports untouched baseline
  differences.
- `bazel test --config=remote //...` was attempted: a frontend CopyFile action
  cannot execute Linux AMD64 coreutils on macOS. Gazelle and its Python manifest
  updater encounter the same host/target executable mismatch. No local Bazel
  execution fallback was introduced.

## Bounded code-quality review

The existing isolated tools were used without adding project dependencies:
Crapper `6fd92c4`, Dryer `66ff6d2`, Mutator `a5a0cb8` (all version 0.1.0).
Complexity was compared with the base revision. New helpers separate trusted
scope, operation parameters, endpoint validation and credential loading.

| New or substantially revised function | Cyclomatic complexity |
| --- | ---: |
| `_context_selection`, `context_snapshot`, `attachment_text` | 9 each |
| `_mail_endpoint`, `_mail_request`, `propose_email` | 8 each |
| `pipeline_decide` | 5 |
| `_mail_service`, `_reply_references`, `history_boundary` | 4 each |
| `_message_time` | 3 |
| `reply_decision`, `record_reply_decision` | 2 each |
| `pipeline_snapshot` | 1 |

Existing dispatchers and workflow functions retain higher complexity, notably
`operate`, `create_app`, `generate` and `pipeline_reply`. They were not fragmented
solely to lower a metric. A passing repository-wide CRAP threshold is not claimed.
Although executable-line coverage was collected, the experimental aggregation
did not reliably preserve uncovered branch records; its CRAP percentages and
scores are therefore not accepted as verification results. Baseline CRAP and
Rust/TypeScript function coverage are unavailable in this run.

Dryer at similarity >= 0.8 found three pairs of short pipeline factory functions.
They are distinct manifest entry points, not duplicated authorization policy,
and were retained.

Mutator scan selected three sites in the required-decision and historical-mail
guards. Those mutations were applied individually with source restoration and
the complete history Bazel suite rerun. All three produced completed failing
tests (Bazel exit 3); these were manually verified rather than counting arbitrary
tool failures as kills. This is a three-site pilot, not a repository-wide mutation
score.
