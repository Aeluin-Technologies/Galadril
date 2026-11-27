# Engineering migration inventory

The baseline is Galadril `fa8e6ae58` plus the verification infrastructure.
The measurements below are from macOS on 2026-11-27. Existing diagnostics
remain blocking. Counts are diagnostic counts and may include multiple
findings on a single line or function.

## Reproduced findings

| Scope | Observation | Follow-up |
| --- | --- | --- |
| Architecture | The MCP workspace contract imports `galadril_plugins.utils.models.DraftInput` in `services/mcp/src/galadril_mcp_service/providers/workspace_contracts.py:7`. | Put the draft input contract on a host-owned interface and keep the plugin implementation behind it. |
| Python suppressions | Three `type: ignore` comments in `machine-learning/galadril-inference/src/galadril_inference/models/text/eru.py:175`, `machine-learning/galadril-inference/tests/storage/test_s3.py:34` and `platform/amarth/tests/test_discovery.py:46`. | Resolve dependency and fixture types, then remove the suppressions. |
| Ruff | The workspace format scan flags `platform/registry/python/tests/test_client.py`. The workspace lint scan produced zero diagnostic records with the existing Ruff configuration. | Format the fixture in the migration. |
| BasedPyright | A local run using the existing application Python environment analyzed 596 files and reported 13,321 errors. The largest categories were propagated `Any` (3,949), unknown members (2,369), variables (1,502) and arguments (1,260), argument incompatibilities (1,126), and explicit `Any` (221). | Resolve imports and dependency stubs first. Then model and validate wire data, focusing on the APIs that propagate untyped values. Rerun with the Bazel environment before using the counts as a release baseline. |
| Dashboard ESLint | The initial typed pass reported 858 errors and 260 warnings. The current dashboard passes typed ESLint and both Vue TSC projects. | Retain typed fixtures and validated wire contracts in the normal test path. |
| Shared UI ESLint | The initial run had 12 errors after generating playground types, including unawaited promises, an unbound method and narrowing assertions. The shared UI lint and type check pass in both verified Linux runs. | Keep the typed fixture and framework boundaries in the existing tests. |
| TypeScript tools | The pilot had 73 typed ESLint errors across Envoy, E2E and frontend tooling. The TypeScript tools target passes in both verified Linux runs. | Keep the request parsing and asynchronous failure checks in the tool test paths. |
| Vue TSC tools | The initial tools project reported `TS2353` for `ssr` under the i18n module options. Both dashboard Vue TSC projects now pass. | Verify runtime behavior with the dashboard browser tests. |
| Rustfmt | `cargo fmt --check` reported diffs in 172 unique Rust files with the local stable toolchain. It also reported that nightly-only formatting options were unavailable. | Reproduce using the Bazel toolchain and separate mechanical formatting from behavior changes. |

The full local `bazel test //...` invocation encountered missing C++
toolchains for `//build/document_compiler:native` and was interrupted when
disk space became constrained.

BuildBuddy Linux analyzed 83 selected Rust targets. Rustfmt rejected files
in Gateway and Intake. Clippy rejected `//services/gateway:gol_upload_test`
for dead code and a redundant struct update, among other findings. The
complete Rustdoc target passed. Re-run the Rust aspect lane as fixes land
to collect the next diagnostics for each target.

The deliberate violations were verified under Bazel on BuildBuddy Linux.
Gazelle regenerated the Python manifest on the Linux runner using the updated
uv lock. The checked-in candidate matches the retrieved artifact. Gazelle's
non-mutating diff still reports repository-wide BUILD metadata drift.

## Consolidated Bazel coverage with Gateway, November 27, 2026

The coverage query preserves the original 288 test labels and adds
`//services/gateway:gateway_test` on a Docker-capable executor. Bazel owns
the combined LCOV. The Linux analysis of `//...` and both initial combined
collections passed with `--lockfile_mode=error`. The two collections ran
289 tests each without reusing test results. The regenerated Bzlmod lock
preserves the Darwin entries and adds the verified Linux Buildx entry.
[Lock verification](https://app.buildbuddy.io/invocation/47c8d628-9e93-43f0-9669-ed1ce1560ec8)
[Collection A](https://app.buildbuddy.io/invocation/8ceb5157-ba6b-487d-8586-7abdc3ac61bd)
[Collection B](https://app.buildbuddy.io/invocation/ed0f5521-910b-4b19-a486-d2da76dc23dd)

Both reports measure 7,251 of 7,931 function entries and identify 222
declarations without runtime bodies. The total number of functions without
evidence drops from 1,676 to 458. Gateway now measures 1,420 of its 1,615
entries, including 784 at zero coverage. The service has 47 missing
functions and 167 scores above 30. The remaining missing evidence includes
70 Rust functions in Document Compiler, 242 Python functions, 46 TypeScript
functions, and the seven Vue functions classified below.

Representative gaps include the Docker `transport_test` in Document
Compiler, the manual `remote_security_test` in Ontology, and browser specs
run by the E2E container outside the coverage query. The canonical LCOV
also lacks a source record for Gateway's `gol_control_plane.rs` fixture,
which enters the compiled test through `#[path]` and `compile_data`.
Those entries remain missing. The two initial reports found 595 and 596
blocking scores respectively. The band change came from a browser fixture
ending before the conversation response rendered. The fixture now waits
for the rendered result. The threshold and the missing-evidence gate remain
in force.

## Verified Bazel coverage before Gateway integration, November 27, 2026

Both uncached test campaigns ran Bazel 9.3.0 against source fingerprint
`34e6e3f4b4b45a6f9b7fa2bd4c51ad58dcf419f7f99704d4e3828ab5df940aa1`
and passed all 288 targets in the CI coverage lane. The BuildBuddy jobs
retain the LCOV, source receipt, CRAP JSON and console logs. Their test
invocations and BuildBuddy artifact hashes were verified upon retrieval.
[Collection A](https://app.buildbuddy.io/invocation/efe7858b-204d-4f49-91a9-083cdf194f30)
and [collection B](https://app.buildbuddy.io/invocation/d004f3bf-c703-490b-a336-755492d80206)
report identical coverage states and CRAP scores for all 7,931 function
entries. The raw counters differ. Among normalized executable points, only
one template branch differs, at `Administration/AccessMap.vue:140`. It
does not intersect a scored script function. The CRAP gate rejects both runs.

| Language | Functions | Sum of CC | LCOV lines hit / executable | CRAP < 10 | CRAP 10–30 | CRAP > 30 | Missing | N/A |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rust | 3,025 | 14,585 | 25,158 / 31,422 | 1,120 | 247 | 91 | 1,381 | 186 |
| Python | 4,130 | 13,066 | 40,564 / 49,669 | 3,206 | 435 | 225 | 242 | 22 |
| TypeScript | 447 | 3,316 | 12,602 / 14,817 | 252 | 65 | 71 | 46 | 13 |
| Vue | 329 | 1,703 | 4,534 / 6,662 | 189 | 82 | 50 | 7 | 1 |
| Total | 7,931 | 32,670 | 82,858 / 102,570 | 4,767 | 829 | 437 | 1,676 | 222 |

Of the 6,033 measured functions, 887 have zero coverage. The Vue LCOV has
170 source records, 164 files with executable lines and 148 with branches.
The seven missing Vue functions are in the three components qualified below.
The two collections have the same executable point identities. Collection A
hits 4,215 of 8,167 Vue branches and collection B hits 4,214. Bazel emitted
line coverage for Rust and line and branch coverage for the other languages.
The CRAP JSON includes the groupings by language, service, platform and
plugin and the individual function evidence. The LCOV percentages apply to
instrumented points only.

The initial inventory counted 381 blocking scores, 747 review scores and
1,974 functions without a mapping, compared with 437, 829 and 1,676 now.
The earlier raw LCOV examined during the audit had no executable lines for
Vue. A unique path-and-function match recovers evidence for 320 previously
unmapped Vue functions. With unchanged source and complexity, 13 functions
initially reported at zero now have executable hits and 321 initial blocking
functions still exceed 30. Test fixtures also changed, so the aggregate
changes include newly exercised paths as well as corrected attribution.

The [full presubmit](https://app.buildbuddy.io/invocation/e5642fb6-c305-4fc2-920d-777f9e57be8c)
includes Docker integration tests outside this coverage lane. The runner
disconnected with 305 confirmed passes, one failure and six E2E targets
without test verdicts. `//infrastructure/kubernetes:ambient_test` requires
a host Docker daemon and failed on the standalone workflow runner. Its local
execution contract remains in place. The six targets without verdicts were
Pengolo execution, proxy startup, pipeline publication, authentication,
dashboard browser and pipeline lifecycle.

A [targeted Gateway coverage run](https://app.buildbuddy.io/invocation/844dda02-af96-48ed-a59e-4ab60f4b7ea0)
passed `//services/gateway:gateway_test` on the same sources. Its Bazel LCOV
has executable points for 1,218 of the 1,265 Gateway functions missing
from both workspace collections. The targeted report measures 1,370 Gateway
functions, with 822 at zero, 180 above CRAP 30, and 97 still missing. The
workspace table continues to report its original invocation scope. The
Gateway comparison matches functions by repository path, line and name.

## Historical Bazel coverage, November 27, 2026

The [BuildBuddy coverage invocation](https://app.buildbuddy.io/invocation/1a49fb98-1cb0-4794-b189-983dd1e42cb9)
ran the CI coverage target set on Linux with Bazel 9.3.0. It passed 287 tests.
`//front/dashboard:ui_lint_test` failed with 858 ESLint errors and 259 warnings.
The report is partial and the gate remains red. The previous seven-failure
run and its C8 zero-coverage inventory predate this measurement.

The BuildBuddy job retains `coverage.lcov`, `receipt.json` and `crap.json`.
The CRAP JSON contains all function rows and grouped summaries by language,
domain and component. The artifact digests and size were verified when
retrieved. Replaying Crapper against the same source fingerprint and LCOV
produced byte-identical JSON. The receipt records exit code 3 and the LCOV
SHA-256 `333f8af438b1e302adf057283893c88e6deec619a03dfad0ed6db87a6536317e`.

| Language | Sum of CC | Measured | Measured at 0% | Missing | N/A | Mean coverage | Mean CRAP | CRAP 10–30 | CRAP > 30 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Rust | 14,585 | 1,458 | 303 | 1,381 | 186 | 74.06% | 14.10 | 247 | 91 |
| Python | 13,058 | 3,864 | 442 | 242 | 22 | 71.77% | 10.46 | 435 | 225 |
| TypeScript | 3,257 | 370 | 28 | 47 | 15 | 63.33% | 71.62 | 63 | 71 |
| Vue | 1,680 | 321 | 111 | 3 | 1 | 49.90% | 29.66 | 80 | 50 |
| Total | 32,580 | 6,013 | 884 | 1,673 | 224 | | | 825 | 437 |

Means are per measured function. A Bazel source record with no executable
points does not establish zero coverage. The most significant Rust gap is
Gateway, with 1,265 missing functions. The existing CI coverage configuration
omits the Docker integration target `//services/gateway:gateway_test`.
The available Rust LCOV has line hits. Python has executable line and branch
records, with module-entry arcs at line zero outside the source.

### Chromium and Vue

The full LCOV contains 169 Vue source records, 164 files with executable
line points and 147 with branch points. The report measures 285 functions
in dashboard SFC scripts and 36 in the shared UI and playground. Three
functions remain unmeasured with empty source records. They are `select`
in `Ai/GraphContext.vue` and `addRole` and `removeRole` in
`Builder/RoleManager.vue`. The two SFCs are present in Nuxt's generated
component catalogue. The client bundling audit checked 116 JavaScript source
maps and found no transformation of either component. Their bodies are
applicable runtime code with missing evidence in the tested bundle.

Chromium test targets passed with the collector enabled. A Chromium run
with the real Vue compiler also demonstrated the states on isolated fixtures.
One function took one of two mapped branches and scored 2.5 at complexity 2.
A second function and a never-loaded emitted module both retained executable
zero-hit points. The invalid source map produced a diagnostic and no
coverage points. The collector regression target covers these contracts
and preserves script coverage when Vue emits out-of-line template columns.

### Dashboard lint and replay validation

BuildBuddy confirms the UI type check, dashboard tools and infrastructure
TypeScript quality targets pass. Two subsequent clean coverage executions
also pass the dashboard lint target. Both failed four Chromium scenarios
within `//front/dashboard:ui_browser_test`. Their WebSocket fixtures omitted
required nullable fields in the Gateway replay envelope. The fixtures now
use `GenerationReplay` and all four scenarios pass in local Chromium.

In both reports from those executions, four additional functions in
`Pipeline/Sidebar.vue` have no coverage points. The production build audit
finds this component, `GraphContext.vue` and `RoleManager.vue` in the Nuxt
catalogue and finds no transforms for them across 116 client source maps.
These seven functions have runtime bodies and remain missing whenever the
coverage invocation provides no executable points for them.
