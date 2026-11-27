# Galadril engineering gates

Run the checks from the repository root. CI uses the checked-in Bazel versions
and passes its BuildBuddy credentials through the existing remote configuration.
The native document compiler and browser tests need the supported Linux
execution platforms.
The CI configuration enforces `--lockfile_mode=error`. Refresh dependency
metadata in a dedicated update and review the resulting lockfile diff.

| Gate | Command |
| --- | --- |
| Behavior, invariants, architecture unit tests, frontend format/lint/type checks | `bazel test //...` |
| Workspace architecture and Python suppression policy | `bazel run //build/quality:check -- architecture` |
| Python Ruff and BasedPyright | `bazel run //build/quality:python_check -- python` |
| Rustfmt, Clippy and Rustc | `bazel build --config=engineering-rust <first-party Rust binary, library and test targets>` |
| Rustdoc and doctests | `bazel build //build/quality:rust_docs` and the normal Bazel tests |
| Intentional Rust violations | `bash build/quality/fixtures/verify-rust.sh` |
| Intentional Python, coverage and architecture violations | `bazel test //build/quality:check_test` |
| Intentional TypeScript and Vue TSC violations | `bazel test //front/dashboard:ui_policy_test` |

The CI workflow selects the Rust target set, runs the checks as required jobs
for publication, and propagates command failures. The gate tests verify named
diagnostics so that setup failures cannot satisfy the deliberate-violation cases.
Architecture checks enforce inward imports in named domain and application
layers, the shared UI boundary, the host/plugin checks in `//tests/architecture`,
and Python and Vue type-suppression directives. Domain modeling, vertical separation,
lifetime design and design-level coupling still require code review.

Ruff and BasedPyright consume the existing uv lock and Bazel Python environment.
The frontend uses the existing pnpm lock and Bazel Node toolchain. Nuxt-generated
typing runs in isolated temporary directories. The UI, dashboard and
infrastructure tools check first-party TypeScript and Vue through typed ESLint,
Prettier, Vue TSC or TSC. Rust uses the existing rules_rust aspects and toolchain.
Generated bindings and vendored HippoRAG sources retain their dedicated build
and contract checks. The intentionally invalid fixtures are checked by the
negative tests. An exclusion or diagnostic exception requires specific evidence
and review, with the original behavior and gate coverage preserved.

## Coverage and CRAP

Run the CI coverage lane from the repository root:

```sh
bash build/quality/collect-coverage.sh /tmp/galadril-coverage --config=ci --config=ci-test
bazel run //build/quality:check -- crap --artifact /tmp/galadril-coverage \
  --output /tmp/galadril-coverage/crap.json
```

The collector removes any stale combined LCOV before coverage. It retains
Bazel's combined report with a receipt containing the invocation ID, Bazel
version, exit code and source and report digests. Crapper verifies both
digests. A report produced with failing tests is partial and keeps the gate
red. Retrieve the LCOV and receipt together when auditing a BuildBuddy run.
The coverage query includes the fast suite and the Gateway integration test
on its Docker-capable executor. Bazel runs both in one invocation and writes
one combined report.

Playwright collects the Chromium V8 profile for locally built test servers.
The [AST mapper used by Vitest](https://vitest.dev/guide/coverage.html) remaps
the exact browser script against a source map whose first-party content is
checked against the staged Bazel input. Istanbul writes LCOV and the existing
Bazel collector merges it with Node, Rust and Python coverage. An emitted
chunk that Chromium never loaded can contribute real zero-hit executable
points from its compiled AST. A missing or invalid transformation produces
a diagnostic in the browser test's undeclared outputs. Vue template segments
with out-of-line columns remain unmapped while verified SFC script points
retain their coverage. Browser fixtures in development mode can
only measure transformed modules for which the profiler obtained evidence.

The pinned [Crapper](https://github.com/unclebob/crapper) readers preserve
Vue script line numbers. Coverage uses mapped branch points when available
and executable lines otherwise. A zero-hit record means measured zero.
A missing mapping has no score and blocks the gate. Declarations without
runtime bodies are explicitly non-applicable. Scores below 10 meet the
target, scores from 10 through 30 require review, and scores above 30 block.
The JSON includes function rows and summaries by language, domain and
component. Service and platform groups follow their owning Bazel packages.
Plugin groups follow the Python plugin packages and plugin utility targets.
Coverage and CRAP means use measured functions. Compare independent runs from
the same source fingerprint with `--nocache_test_results` and inspect per-function
differences before treating a change as application test debt.

The current validation state is recorded in [the engineering inventory](INVENTORY.md).
