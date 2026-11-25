# CI workloads and measurements

| Workload | Invocation | Test result reuse |
| --- | --- | --- |
| Pull requests and main | `bazel test //... --config=ci --config=release --config=ci-test --config=ci-presubmit` | Bazel reuses eligible results. The transversal lifecycle, authentication, authorization, publication, GOL browser and Pengolo checkpoint tests run against fresh services. |
| Weekly or manually dispatched validation | `bazel test //... --config=ci --config=release --config=ci-test --config=ci-validation --keep_going` | All tests tagged `integration` run with `--nocache_test_results`, including the live empirical and assistant suites. |
| Manually dispatched benchmark | `bazel test //tests/e2e:pipeline_lifecycle_benchmark --config=ci --config=release --config=ci-test --config=ci-benchmark` | No test result reuse. Runs all 240 retail and 205 trial observations and every lifecycle stage inside the same fresh environment. |
| Release database validation | `bazel test //database/tests:image_test --config=ci --config=release --config=ci-push` | Shares release artifacts and tests both architectures, SBOMs and provenance in an independent job. |
| Pull request line coverage | `bazel coverage //... --config=ci --config=ci-test --config=ci-coverage --combined_report=lcov --instrumentation_filter=//... --instrument_test_targets` | Instrumented unit, contract and browser coverage. The integration executions belong to the test jobs. |

The full integration validation and benchmark can be dispatched with the
**Bazel integration validation** workflow. Both retain compilation and OCI
artifact caching. Release publishing keeps its stamped tags. Test invocations
use release compiler settings with `--nostamp`. The functional lane retains
the native AMD64 extensions test. The multiarchitecture job uses the same
stamped release configuration as publishing. Publishing requires both jobs. The workspace browser target
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

## BuildBuddy audit samples

| Invocation | Wall time | Critical path | Remote cache hits | Remote executions | Test targets executed |
| --- | ---: | ---: | ---: | ---: | ---: |
| [Linux lifecycle reference on main](https://app.buildbuddy.io/invocation/d150d0cc-5c06-42fb-b608-631230927729) | 1,866.77 s | 1,786.27 s | 2,563 | 10 | 5 of 5 |
| [Initial PR workspace, stopped by BuildX](https://app.buildbuddy.io/invocation/9f8c97fe-f315-4ab2-a4f4-101f9d855ae5) | 163.68 s | 55.22 s | 1,819 | 195 | 39 of 279 |
| [Isolated database on main, aligned flags](https://app.buildbuddy.io/invocation/59e8830a-14ca-4602-a352-e5ab2aa2e647) | 785.54 s | 784.64 s | 4 | 4 | 1 of 1 |
| [Isolated database with the retry patch, aligned flags](https://app.buildbuddy.io/invocation/40ef3750-1e78-4c4c-8f69-ef927c3f21e9) | 440.74 s | 439.61 s | 3 | 5 | 1 of 1 |
| [Four contract tests, initial run](https://app.buildbuddy.io/invocation/0befb8cc-5ff0-4d8e-bf57-7e60af8f9957) | 6.44 s | 5.69 s | 2 | 6 | 2 of 4 |
| [Same tests and commit, warm server](https://app.buildbuddy.io/invocation/4a6a079c-37a1-4060-a75a-7f37b6611f63) | 0.15 s | 0.02 s | 0 | 0 | 0 of 4 |
| [Pipeline definition change](https://app.buildbuddy.io/invocation/32b646c3-c499-4a2d-8da5-b48e0b86dc72) | 5.21 s | 5.05 s | 0 | 2 | 1 of 4 |
| [Compose fixture change](https://app.buildbuddy.io/invocation/2b2c7a91-5bd7-41a4-b9db-5a2e35af8d0e) | 9.80 s | 9.62 s | 2 | 4 | 2 of 4 |

Bazel reports remote executions and remote cache hits separately. The remote
execution count is not an action-cache miss counter, and elapsed time does
not measure aggregate resource consumption. Both database samples above
passed. The main sample logged a resumed connection reset, and both cold
BuildX timings vary with registry availability. The first workspace run
stopped early, so its short elapsed time cannot support a speed comparison.

`cquery` traces the ARM64 input through
`//database/tests:image_test → //database:image → //database:attested_arm64 → //database:base_arm64`.
The native extension test uses only `//database:base_amd64` on x86-64.
The E2E targets pull the pinned PostgreSQL runtime and have neither BuildX
target in their configured graphs. `aquery` showed that `--nostamp` changed
the transitioned BuildX input and output paths at the first CI run. The
release verification target inherits the release flag and shares both
BuildX action keys with `//database:push`.

`bb explain` attributed the pipeline probe to the changed runfile and one
invalidated test. The Compose probe re-executed the Compose model and E2E
support contracts. Comparing the original PR run to the isolated database
run also showed that the retry wrapper changed the BuildX command itself.
The connection reset is visible on main as well as on the PR.

The database base image, Dockerfile frontend, BuildKit and the Syft scanner
are digest-pinned. AGE and pg_textsearch sources use fixed revisions.
Package installation still reads the upstream APT repositories on a cold
build. Cache reuse and verification against the published image digest
therefore do not establish bit-for-bit reproducibility across cold builds.
