# CI workloads and measurements

| Workload | Invocation | Test result reuse |
| --- | --- | --- |
| Pull requests and main | `bazel test //... --config=ci --config=release --config=ci-test --config=ci-presubmit` | Bazel reuses eligible results. The transversal lifecycle, authentication, authorization, publication, GOL browser and Pengolo checkpoint tests run against fresh services. |
| Weekly or manually dispatched validation | `bazel test //... --config=ci --config=release --config=ci-test --config=ci-validation --keep_going` | All tests tagged `integration` run with `--nocache_test_results`, including the live empirical and assistant suites. |
| Manually dispatched benchmark | `bazel test //tests/e2e:pipeline_lifecycle_benchmark --config=ci --config=release --config=ci-test --config=ci-benchmark` | No test result reuse. Runs all 240 retail and 205 trial observations and every lifecycle stage inside the same fresh environment. |
| Pull request line coverage | `bazel coverage //... --config=ci --config=ci-test --config=ci-coverage --combined_report=lcov --instrumentation_filter=//... --instrument_test_targets` | Instrumented unit, contract and browser coverage. The integration executions belong to the test jobs. |

The full integration validation and benchmark can be dispatched with the
**Bazel integration validation** workflow. Both retain compilation and OCI
artifact caching. Release publishing keeps its stamped tags. Test invocations
use release compiler settings with `--nostamp`. The workspace browser target
uses a digest-pinned Chromium runner. Its local equivalent is still available
by label for development.

## Reference audit

The reference is `6aefd863ba84612bae08a755c5e1be7562b8cd03`. Configured
`cquery deps(//tests/e2e:pipeline_lifecycle_test)` on Linux x86-64 emitted
47,619 entries including configuration variants. `aquery` identified a single
`TestRunner` action consuming eight `OCITarball` actions. The same image list
was declared for the GOL browser and Pengolo checkpoint targets.

- GOL exercised the Gateway and Registry, pulled all eight images and started
  Intake and Scribe through the shared startup path. The checkpoint target
  needs Scribe/MCP and Vision's schema but sends no ingestion input.
- The common Python support target attached browser sources, ontology
  configuration, database and Compose inputs to callers that only parse
  assertions or test Ory login. Retrieval and assistant helpers shared the
  empirical and browser dependencies. A separate local browser target
  duplicated specs in the remote workspace target. The workflow exported a
  runner-specific Chromium path across the whole workspace.
- The retail retrieval oracle reads the identities and source rows actually
  produced by Vision. Trial authorization uses the same empirical principals.
  Both remain together so the test cannot pass with synthetic identity rows
  or a previously populated database. Chatbot and Pengolo keep a single fresh
  causal cohort for their shared provenance and revocation checks.
- Compose used a mutable database tag, other version tags and an ambient
  Docker daemon. Browser preparation still fetches npm dependencies and can
  install a system package. The Compose dependencies in these suites now
  use immutable image references. The corresponding OCI application images
  are Bazel inputs. Tests using mutable runtime dependencies carry `no-cache`.
- BuildBuddy launches Docker tests in isolated Firecracker workers, with
  disk and compute reservations for Vision and Ray. Compose project names
  are unique and state volumes are removed on exit. Local fixed-port Compose
  targets use `exclusive-if-local`. Use a separate daemon when running multiple
  local Bazel invocations against these same loopback ports.

## How to compare builds

Keep the commit, runner class, Bazel version, platform, release flags and target
set in the measurement record. Compare the five reference labels
`pipeline_lifecycle_test`, `gol_lifecycle_test`, `pengolo_execution_test`,
`authentication_e2e_test` and `e2e_support_test` with
`//tests/e2e:pipeline_integration_tests` after the split. Compare the presubmit
subset separately because it runs a smaller workload.

```sh
bazel cquery --config=ci --config=release --config=ci-test \
  'deps(//tests/e2e:pipeline_lifecycle_test)' --output=label
bazel aquery --config=ci --config=release --config=ci-test \
  'mnemonic("TestRunner|OCITarball", deps(//tests/e2e:pipeline_lifecycle_test))'
bb explain --old "$OLD_INVOCATION" --new "$NEW_INVOCATION"
```

Record the compact execution log with `--execution_log_compact_file`. Use the
Bazel timing profile and BuildBuddy cache statistics for critical path, action
cache reads, bytes transferred and remote execution. Count targets from BEP
test summaries. Use the sum of execution durations or CPU usage across
actions for cumulative cost and keep it separate from elapsed time. The E2E
test logs report individual OCI imports and the existing OTLP assertions
correlate Gateway, Intake and Vision spans.

For repeat and localized-change comparisons, keep the target set fixed.
A change to the standalone HotpotQA fixture should invalidate its oracle and
the empirical retrieval consumer. A change to the GOL Chromium spec should
change only the corresponding browser runfiles in the lifecycle suite.
Container test actions still execute because they carry `no-cache`. Unchanged
Python fixture targets and image inputs can reuse results. Bazel keys downstream
actions on the content of generated inputs when an upstream action reruns.
