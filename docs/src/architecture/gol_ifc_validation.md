# Contextual IFC validation record

Validated locally on macOS arm64, 16 November 2026. This record covers the
GOL library and disposable test infrastructure, not a production deployment.
The [integration audit](gol_ifc.md) identifies remaining application boundaries.

## Results

| Gate | Result |
| --- | --- |
| Bazel GOL suite, explicit Outlines, Docker and Verus targets | 14 targets passed |
| Real RustFS/SpiceDB/Cedar/IFC matrix | Five tests passed, including the isolated worker subprocess |
| Explicit RustFS bucket-precondition Docker test | Passed |
| Verus `--no-cheating --compile` | 12 verified, zero errors |
| Generated IFC algebra cases | 262,144 label pairs and deterministic triples passed |
| Focused IFC effect/admission regressions | 13 passed |
| Cargo integration tests with `export,remote` | Passed; Docker tests are separately invoked |
| Rustdoc, including checked wire API compile-fail case | Eight passed |
| Workspace Clippy, all targets/features, `-D warnings` | Passed |
| Scoped nightly rustfmt check | Passed |
| Runtime-only build without parser | Passed |
| Workspace `bazel test //...` | Blocked by missing C++ toolchain for `//build/document_compiler:native`; 30 passed, 109 skipped |
| Workspace nightly rustfmt | Existing differences in Gateway `iam_store.rs` and `directory_label.rs`; left outside this change |

## Security scenarios

| Scenario | Observed result |
| --- | --- |
| Read above clearance or without Legal compartment | Refused; failed calculation cannot write or issue a live label snapshot |
| Sensitive read followed by dependent public mutation | Refused; existing RustFS object retained its original bytes |
| Public property update with public inputs and high maximum clearance | Actual write committed |
| Confidential computation writing a Restricted property | Actual protected object committed |
| Public operation in another isolated process with fixed public inputs | Actual public file committed while the parent retained its sensitive label |
| Fork after sensitive observation or attempted label reset | Public mutation refused; original object retained |
| Secret read plus public action write, irrespective of route order | Callback not invoked; destination remained Public |
| Blind write to a protected destination | No artificial read dependency; status retained in opaque receipt |
| Compound field mutation using only parent grant | Refused; protected child file unchanged |
| Cedar private resource/context predicate | Denied result retains its control label |
| Clearance revocation during a warm calculation | Callback not invoked; calculation poisoned |
| Cedar region change at warm cache | Effect refused |
| SpiceDB revocation with `at_least_as_fresh` | Receipt inspection and deletion refused; stored object unchanged |
| Foreign tenant, LLM transfer or unregistered Kafka sink | Refused before the effect callback |
| Denied metadata versus unknown candidate | Identical public schema; hidden candidate does not poison public filtering |
| Cross-calculation receipt observation | Refused |
| Corrupt archives, indirect links/views and partial projections | Existing real-engine regression matrix passed |

The Kafka case checks the release hook's fail-closed behavior for an
unregistered sink; it does not launch a broker. The process test establishes
one deliberately restricted input boundary and does not prove that arbitrary
Gateway, Ray or plugin dispatch is isolated. Timing/covert channels and
non-interference of host code are not established.

## Reading measurements

Representative loopback measurements from the successful Docker suite:

| Workload | Measurements |
| --- | --- |
| Authorized remote JSON schema | Archive 95,360 bytes; cold 13 requests / 22,720 bytes; warm zero additional requests |
| IFC effect matrix | 14,380 bytes read from a 30,956-byte artifact |
| Large fixture, repeated full downloads | 75,297,336 total network bytes |
| Same fixture, uncached sparse reads | 2,587,416 total network bytes |
| Same fixture, cached sparse reads | 40,204 total network bytes; 1,063 RAM cache hits |

Benchmark totals cover repeated operations and include manifests; they are
not individual artifact sizes or a production latency guarantee. Sparse reads
save bytes but can make more HTTP requests without caching. No claim of
universal latency improvement follows from these measurements.

## Reproduction

Use the normal repository Bazel configuration and installed host toolchains:

```sh
bazel test //platform/ontology/rust:all \
  //platform/ontology/rust:export_outlines_test \
  //platform/ontology/rust:remote_security_test \
  //platform/ontology/rust:ifc_verus_test \
  --test_env=VERUS_BIN --test_env=RUSTUP_HOME --test_env=PATH
bazel test //platform/ontology/rust:remote_test --test_arg=--ignored
cargo +nightly fmt -p galadril-ontology -p golc-wire --check
cargo clippy --all-targets --all-features -- -D warnings
```

`VERUS_BIN` points to the pinned official verifier and `RUSTUP_HOME` to its
Rust 1.98.1 installation. Docker is required for explicit container targets.
The macOS run used a temporary Bazel output root, disabled the disk cache,
used `--lockfile_mode=off`, and reused the installed Cargo cache with
`--repo_env=CARGO_BAZEL_ISOLATED=false --repo_env=CARGO_NET_OFFLINE=true`.
This preserved unrelated workspace lockfile edits. The ignored HTTP socket
fault-injection unit test was not included in this validation pass.
