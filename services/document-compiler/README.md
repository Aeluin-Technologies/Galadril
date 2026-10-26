# Document compiler

See [the architecture and deployment guide](../../docs/src/architecture/document_compiler.md).

- `domain`: durable job DTOs.
- `ports`: object storage, authority, queue and isolated engine contracts.
- `application`: immutable templates, queue orchestration and retention.
- `adapters`: S3/RustFS, Gateway delegation callback, Diesel/libSQL and Unix IPC.
- `grpc`: bounded private Tonic protocol.
- `worker`: memory-only Tectonic engine and trusted public rootfs installer.
- `supervisor` / `sandbox_policy` / `kernel`: POSIX process lifecycle, Landlock, seccomp and quotas.

No default template or credential is embedded. The worker requires an operator
provided offline TeX bundle. The service intentionally fails closed without its
Gateway authorization callback, bucket, durable queue or Linux supervisor.

Validation evidence and outstanding deployment requirements are recorded in
[VALIDATION.md](VALIDATION.md).
