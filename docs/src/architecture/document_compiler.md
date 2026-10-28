# Document compiler

The compiler is an optional native service. Gateway remains the authority for
identities, SpiceDB/Cedar decisions, live Pengolo generation capabilities,
libraries, downloads and retention. Scribe supplies native PydanticAI tools;
Registry continues to own ontology and pipeline revisions. The compiler does not
introduce another IAM, user database, library or plugin runtime.

## Request and artifact flow

1. `document_templates` lists revisions visible to the current generation.
   `read_document` reads the entrypoint or a selected CLS/STY/TeX file in bounded
   pages. Template content is untrusted source data, never agent instructions.
2. Templates are complete virtual projects, including relative assets, an
   entrypoint, typed variable declarations and metadata. Text substitutions use
   `<<galadril:variable>>`; ordinary LaTeX groups such as `{{#1}}` remain intact.
   Text/choice values are escaped, integers and booleans retain their types.
   A raw `source` replaces the entrypoint for authoring against an inspected CLS.
3. Gateway forwards the existing opaque generation capability over private gRPC.
   It never gives Scribe storage credentials, queue access or object keys. Each
   operation resolves the persisted live generation and its canonical actor.
4. The service checks template authorization before fetching its manifest, then
   checks its sealed metadata and current library scope. Immutable versions are
   SHA-256 digests of the complete validated manifest. Publication and irreversible
   deletion are separate markers. Tenant prefixes are derived from authority.
5. `compile_document` submits one to eight jobs with stable per-tool-call UUIDs.
   Diesel/libSQL persists only bounded queue state, leases and the original
   delegation; source projects and PDF bytes are in the private S3/RustFS bucket.
6. Two Tokio worker tasks claim fenced leases. They recheck current use permission
   during execution and before storing output. Cancellation drops the direct
   supervisor future and kills and reaps the isolated process. Restart recovery
   claims expired leases, with at most three attempts; it does not bypass expired delegations.
7. Gateway fetches completed output after another permission check and publishes
   through its existing staged Pengolo upload and retention mechanisms. One result
   is a PDF. Multiple jobs are exported as one ZIP. Selected template source files
   can also be exported as a bounded ZIP. Downloads use authenticated Gateway
   URLs. A model cannot choose a library outside the generation's scope.

An empty catalog is valid. Personal templates retain their owner boundary, even
for tenant administrators. Project templates require the appropriate project
library, while tenant templates require tenant administration for creation.
Explicit `view`, `use`, `edit`, `publish` and `delete` relationships reuse the
existing AccessStore and fully consistent authorization checks.

## Official plugins

A reviewed `/plugins` package may declare `document_templates` as a list of
relative packaged JSON resources in `plugin.json`. Each resource follows
`galadril_plugins.document_templates.TemplateDraft`: UTF-8 or base64 assets,
relative paths, typed variables and metadata. No executable factory is imported
while discovering these resources. There are no built-in production templates.

Scribe exposes the deployment inventory on its Gateway-only private endpoint.
Gateway offers `plugin_catalog` and `enable_plugin` through `document_templates`.
Tenant administrators explicitly enable a packaged plugin. Visible templates are
then imported lazily into that tenant's object prefix, with deterministic plugin
identities and immutable content versions. Other tenants need their own activation.
Plugin provenance comes from the installed package; native template writes cannot
impersonate it. Disabling the plugin removes the live SpiceDB use/view grant;
removing its package also denies access. Existing tombstones are not resurrected.

## Isolation and quotas

The API and supervisor share one trusted Rust process and Tokio runtime. The
application calls the supervisor directly through its `Sandbox` port; there is no
API-to-supervisor socket, supervisor service or supervisor image. The API owns bucket
credentials and the libSQL queue. A separate worker image is used only by a
trusted init container to prepare the public runtime; it does not remain running.
Each compilation starts a fresh engine process with no telemetry exporter,
network client, service configuration or delegated credentials.

On Linux, `nix::spawn::posix_spawn` starts a fresh instance of the Rust worker
with constant arguments, a minimal environment and its own process group. No
shell or external LaTeX compiler runs. Before decoding the untrusted project,
the single-threaded worker closes inherited descriptors, disables dumps, installs
hard resource limits, requires Landlock ABI v3 in fully enforced mode, and installs
a default-kill seccompiler BPF filter. Missing kernel support prevents readiness;
there is no permissive fallback.

Landlock permits reading only the public runtime and the current job directory;
only the latter is writable, and neither is executable after restrictions apply.
The filter denies networking, process creation, exec, ptrace, mount, namespace
changes and changes to the sandbox policy. The worker runs as UID 1000 with no
capabilities and no-new-privileges. Process limits are 1 GiB virtual address space,
32 MiB per file, 64 descriptors, no core files and 40/45 CPU seconds. Tokio kills
and reaps the process group after 60 wall-clock seconds or cancellation of the
compilation future.
Parent death also sends SIGKILL. There are at most two children; the combined API
container bounds their combined CPU, memory and temporary storage. Further
process creation is forbidden after restrictions apply; Compose additionally
sets a container PID limit.
Admission is bounded to 256 active jobs and 32 per tenant. Projects are at most
2 MiB; PDF/ZIP output is at most 16 MiB.

This is deliberately not equivalent to libcontainer: workers inherit the
API container's namespaces and cgroup instead of having separate ones per
job. Per-process rlimits do not provide individual cgroup CPU scheduling or
per-job disk accounting. Landlock and seccomp provide the intra-container boundary;
the restricted container and dedicated node pool bound its blast radius. A kernel
vulnerability remains a residual risk. The supervisor now shares the API's
filesystem and aggregate CPU/memory budget; it is trusted code, not a sandbox
boundary. Workers receive a fresh minimal environment and close inherited file
descriptors before reading project input. Landlock excludes the queue, credentials,
parent `/proc` files and other job directories. Unlike the previous two-container
topology, there is no separate mount namespace or cgroup between the API and its
workers. A successful kernel/sandbox escape could expose API secrets, and aggregate
memory exhaustion can restart the whole service; durable leases recover afterward.
Tenant isolation additionally relies on Gateway's
live authorization and server-derived storage prefixes; it is not delegated to
LaTeX path validation alone. See the upstream
[Landlock limitations](https://docs.kernel.org/userspace-api/landlock.html#current-limitations)
and [seccompiler documentation](https://docs.rs/seccompiler/0.5.0/seccompiler/).

Tectonic 0.17 is linked directly, uses in-memory project assets and output, and
has shell escape disabled. Packages requiring external Biber, shell commands or
external converters are unsupported and fail within the kernel policy; no child
process may be launched by an engine. Native libraries require independent processes; a
Tokio timeout alone cannot stop a wedged C engine. Assets share reference-counted
buffers within the worker. Serialization across a process boundary and bounded
archive construction intentionally copy bytes to preserve isolation.

## Storage and seven-day retention

Template revisions are write-once S3 objects. They are not stored in libSQL.
Diesel 2.3 with diesel-libsql 0.1.4 provides typed local libSQL queries; only adapter
migration and transaction-control statements use raw SQL. WAL, busy timeout,
`BEGIN IMMEDIATE` admission/claims and lease fencing protect concurrent controllers.
The local queue file must be on durable storage. Remote replicas are not used for
lease reads, because stale reads cannot fence workers.

Compiler job artifacts expire seven days after admission. A background task
purges expired terminal jobs, including current S3 objects, old versions and
delete markers, then deletes the queue row. Gateway publication sets a separate,
non-sliding seven-day deadline using its existing retention system. Reading a
generated artifact does not renew that deadline. Temporary-chat deletion can
remove an artifact earlier. Storage outage or downtime can delay physical
collection; access expiration remains enforced.

Configure a bucket lifecycle rule for the tag
`galadril-retention=seven-days`, including noncurrent-version expiration and
expired delete-marker collection. Compiler job input/output and published
Pengolo artifacts carry this tag. This is also required to collect inputs left by
a process crash between writing S3 and admitting the queue row. Do not apply this
rule to template revisions. Gateway's staging bucket keeps its existing short
staging lifecycle. No download URL grants seven days of unauthenticated access.

## Deployment

`infrastructure/docker/document-compiler.yaml` is a Linux-only opt-in Compose
overlay. `infrastructure/kubernetes-overlays/document-compiler` is the Kubernetes
entrypoint; it includes the normal Galadril base and a separate
`document-compilation` namespace. Galadril's restricted namespace is unchanged.
Both namespaces enforce restricted PodSecurity. The combined compiler and
trusted initializer run without capabilities. Deploy compilation only on dedicated
labeled Linux nodes with Landlock ABI v3 (Linux 6.2 or later), seccomp and a
container runtime policy that permits installing these restrictions. The
in-process supervisor startup probe verifies the actual kernel policy.

Before enabling either deployment:

- Build and publish the combined API and worker initializer images with the
  normal Bazel OCI targets. Pin their deployed references to reviewed image digests.
- Prepare an offline **TTB v1** TeX bundle as `bundle.ttb`, with its exact
  SHA-256 digest. The [local CLI](../../../infrastructure/dev/README.md) downloads
  and verifies the pinned complete official archive automatically. Kubernetes
  uses a public-resource init container without secrets. The trusted rootfs
  init container verifies the generated digest and copies only
  public runtime libraries and the worker. It mounts no application secrets.
  The bundle is the TeX distribution, not a tenant template or user catalog.
  [Tectonic's bundle documentation](https://github.com/tectonic-typesetting/tectonic/blob/master/bundles/README.md)
  describes its offline format and construction.
- Provision the queue PVC and bundle PVC, and the `document-compiler-storage`
  Secret shown in the overlay. The [production overlay](../../../infrastructure/kubernetes-overlays/production/README.md)
  obtains application credentials through Vault Secrets Operator and public TLS
  certificates through cert-manager. The bundle initializer publishes its digest
  beside the public bundle; no manually supplied digest ConfigMap is required.
  Use a dedicated private artifact bucket and scoped object/list/version-delete
  credentials. Configure the retention lifecycle above.
- Preserve the combined container resource limits (3 CPUs and 3 GiB) and the
  bounded temporary filesystem. No host cgroup mount, privileged container or unconfined security
  profile is required. The local Compose overlay uses a trusted, networkless
  initializer to prepare named-volume ownership; it never handles tenant data.
  Recreate its public runtime volume when upgrading the worker image or bundle.
- In Kubernetes, Ambient strict mTLS and compiler/Gateway waypoint policies bind
  RPC and callback paths to the corresponding workload service accounts. The API
  uses ambient mode explicitly. In standalone Envoy deployments, use
  `infrastructure/envoy/document-compiler.yaml`, the generated compiler identity,
  and Gateway's loopback compiler egress. Compose uses the existing trusted
  shared proxy namespace, as other local services do.

The API becomes ready only after bucket, queue and fresh restricted-worker checks.
SIGTERM stops admission and cancels active execution. OTLP spans, logs, job counts and
duration come from the combined API using the repository telemetry crate. Raw
LaTeX, template bytes, credentials and native error output are not logged.

## Native build compatibility

The remote executors do not provide Tectonic's development libraries. Pinned
official Ubuntu packages provide both architecture-specific native archives and
runtime libraries, with SHA-256 verification and license files in the image.
Header-only Cargo metadata is generated from those declared Bazel inputs; no
executor package manager runs. Three narrow upstream build-script patches copy
Tectonic exported headers into declared output directories. A rules_rust patch
preserves producer output paths in `DEP_*` metadata; version 0.71.3 otherwise
replaces them with an unresolved consumer `${out_dir}` token. These patches affect
build integration, not the engine's runtime security policy.
