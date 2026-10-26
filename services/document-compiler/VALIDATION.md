# Validation and operational boundaries

Validation uses synthetic templates and credentials. The supplied private legal
examples were inspected as format references and were not copied into the repo.
No production catalog is seeded. No images or deployment have been published.

## Execution model

All Bazel builds and tests use BuildBuddy with `--config=remote`. Integration
tests use the repository's Docker/Firecracker execution properties. Networked
build actions use the existing `//build:rbe_network_linux_x86_64` and ARM64
platforms, without switching to local execution. Credentials are supplied only as
private request headers and are not recorded here.

The standard Linux executor supports mandatory Landlock ABI v3. The nested
Firecracker Docker executor does not: the actual worker image refuses its kernel
policy before reading project bytes. The container compatibility test requires
either fully enforced readiness or this explicit refusal; it never accepts a
permissive fallback. Real native PDF and attack tests run on the compatible
standard executor using the same production kernel restrictions.

## Executed feature checks

| Check | Evidence |
| --- | --- |
| Native engine | A real PDF with a project-provided CLS and macro; two concurrent processes produce identical PDFs. |
| Multiarch images | The API, supervisor and worker OCI indices and their AMD64/ARM64 images build successfully on BuildBuddy; ARM64 execution is not claimed. |
| Scribe and plugins | All 37 targets pass after integration corrections; the model E2E target also passes. |
| Kernel attacks and supervision | Other-job and system-file reads denied; public runtime writes denied; oversized address-space reservation denied; network syscall kills the worker; environment credentials removed; cancellation and the 60-second watchdog kill and reap blocked workers. |
| Private transport and storage | Actual Tonic client/server, MinIO S3 and local libSQL exercise immutable revisions, tenant separation, eight concurrent idempotent submissions, revocation, tombstones and object deletion. This test uses a timing-controlled engine fixture; native rendering is tested separately. |
| Durable queue | Atomic leases, independent concurrent controllers, restart recovery, idempotence conflicts, cancellation, tenant boundaries and retention. |
| Gateway | The 268-test Gateway suite includes real PostgreSQL and SpiceDB checks, separate template permissions, cross-tenant denials, plugin activation/revocation and atomic artifact reservation. |
| Envoy | Real proxy tests cover compiler workload identity, denied foreign identities, exact RPC paths and the private authorization callback. Contract tests validate route configuration. |
| Shared domain and archives | Typed variables, text escaping, ordinary LaTeX macro groups, source/path attacks, immutable digests and bounded ZIP round trips. |

The transport tests use MinIO for S3 compatibility; they do not claim a live
RustFS deployment or a hosted Turso test. The typed queue uses local libSQL WAL
on its durable PVC. These checks do not claim a deployed Pengolo-to-PDF cluster
scenario. Runtime readiness remains mandatory on the actual target nodes.

## Coverage and isolated executables

LLVM coverage instrumentation links a profile writer that runs when a native
executable exits. Its filesystem/syscall requirements conflict with the worker's
mandatory kernel policy, so an instrumented worker can receive SIGSYS even after
producing a valid PDF. This also makes a network-denial assertion ambiguous if
termination happens in the profile writer instead of the forbidden network call.

The `isolated_artifacts` build transition disables coverage only for the engine,
attack probe and diagnostic executable and their dependencies. The same engine
artifact is packaged in the worker image. The Python integration test, API,
supervisor and ordinary Rust unit tests retain their normal coverage settings.
Native engine runtime lines are therefore not measured by LLVM coverage; real
PDF, concurrency and kernel attack tests still execute under the unchanged
Landlock/seccomp/resource-limit policy. A regression check rejects the LLVM
profile writer in all three restricted executables.

The 12 compiler/domain targets pass with `bazel coverage`,
`--instrumentation_filter=//...`, `--instrument_test_targets` and
`--combined_report=lcov` on BuildBuddy. The same 12 targets also pass in ordinary `bazel test` mode. The worker suite
executes seven tests.
The combined LCOV report retains executed lines for the application, queue,
supervisor and shared domain; this is not a package-wide coverage exclusion.

## Required repository checks

- `cargo clippy --all-targets --all-features -- -D warnings` passes. Linux worker
  and supervisor Clippy also passes through the dedicated Bazel `native_clippy`
  target with warnings denied.
- `uv run --no-project --with ruff==0.16.5 ruff format --check` and the matching
  `ruff check` invocation pass across the Python workspace.
- Both Gazelle commands are built remotely and executed inside a networkless
  Linux container because their generated runners are Linux executables on this
  macOS host. Unrelated generated formatting changes are discarded.
- `cargo fmt --check` is executed but fails: the repository rustfmt configuration
  requires unstable nightly options. With nightly, the changed Rust files are
  formatted; the workspace check still reports pre-existing differences in
  Gateway `database/iam.rs`, `ports/iam_store.rs`, `usecases/audit.rs`,
  `domain/agent_activity.rs` and `domain/directory_label.rs`.
- `bazel test //...` is executed with remote execution. On this macOS client,
  existing frontend copy actions and local-tagged browser/Ambient targets try to
  execute Linux tools locally and fail with executable-format errors. The full
  check is not reported as green. The additional `--keep_going` run reached
  201 passing targets; its remaining
  long E2E scenarios were interrupted after identifying Scribe initialization
  regressions. All 37 Scribe/plugin targets pass after correcting those
  regressions. The 13 targeted compiler/domain/model targets also pass. The
  full macOS workspace result remains failed.

Compose configuration merges successfully with the existing base using synthetic
environment values. `kubectl kustomize` assembles the opt-in overlay. Neither
check creates or modifies a deployment.

## Deployment prerequisites

Deploy only on dedicated labeled Linux nodes with Landlock ABI v3 and seccomp.
Keep restricted PodSecurity, read-only public runtime mounts, no capabilities,
separate API/supervisor filesystems and the container resource limits. This is a
Landlock/seccomp sandbox inside a restricted container; it does not create
per-compilation namespaces or cgroups and does not eliminate kernel risk.

Provision the offline TTB v1 bundle and its SHA-256 digest, a durable queue PVC,
scoped private bucket credentials and the seven-day lifecycle for tagged current
and noncurrent job/artifact objects. Templates must be excluded from that
lifecycle. Application access deadlines are fixed; physical collection can be
delayed by storage outages or downtime. See the architecture guide for the full
request flow and retention policy.
