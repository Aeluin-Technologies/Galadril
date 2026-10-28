# Local development

Install [uv](https://docs.astral.sh/uv/) and Docker with Compose 2.24.4 or newer.
The Docker engine must run Linux 6.2 or newer with Landlock ABI v3. On macOS,
this requirement applies to the Docker VM. Allow sufficient disk space for the
application images and the complete TeX resource bundle (about 3 GB downloaded,
1.4 GB retained). Native Windows is supported through WSL.

From a fresh clone:

```sh
uv run infrastructure/dev/galadril.py
```

The interactive menu offers startup, configuration, diagnostics, tenant creation
and shutdown. It can ask for an OpenAI key without echoing it and explicitly
install the generated public CA in the workstation trust store. No OpenSSL,
machine-learning Python environment, manually created certificates or copied
`.env.example` is required. Docker images must be accessible to the workstation.

For scripted setup:

```sh
uv run infrastructure/dev/galadril.py init --no-input
uv run infrastructure/dev/galadril.py up --no-input --trust
uv run infrastructure/dev/galadril.py tenant --email demo@galadril.test
uv run infrastructure/dev/galadril.py doctor
```

`tenant` prompts for a password of at least 12 characters and creates an isolated
test organization and its administrator through Kratos and Gateway's existing
provisioning API. It does not implement a separate IAM system or change database
rows directly. Sign in at <https://localhost:8080/login>. Enrollment is idempotent;
after deliberately deleting test volumes, use `--recreate-missing` to replace an
enrollment only when Kratos confirms that its old identity no longer exists.

Use `--openai-key-file /absolute/private/key` or `--password-file
/absolute/private/password` for unattended input. These files must have mode
`0600`. Never put secrets in command arguments. The OpenAI key is optional;
Pengolo needs a configured model provider before generation can work.
Supplying an OpenAI key selects the reviewed catalog in `model-catalogs.json`.
Without a key or an explicit model configuration, the generated catalog is empty.
An explicit `SCRIBE_MODELS`/`SCRIBE_DEFAULT_MODEL` pair is preserved, including
local providers supplied through `--import-env`.

All generated configuration lives in ignored, private `.local/dev/`: certificates,
signing keys, public JWKS, Compose and connector configuration, plugin credentials,
enrollment state and the Tectonic bundle. No application receives the CA signing
key or the local JWT signing key. A local diagnostic token is written to a private
file rather than stdout. It establishes identity only; Gateway still checks IAM.
Local signed tokens are never part of the Kubernetes configuration.

Repeated `init` and `up` preserve generated credentials and private keys. Existing
`infrastructure/docker/.env` files are not loaded automatically. Explicit
`--import-env /absolute/private/legacy.env` imports an existing installation;
values supplied there override saved settings. Import matching database/storage
credentials when retaining old volumes. Do not rotate passwords underneath an
existing database by deleting generated configuration.

```sh
uv run infrastructure/dev/galadril.py status
uv run infrastructure/dev/galadril.py certificates
uv run infrastructure/dev/galadril.py up --no-pull
uv run infrastructure/dev/galadril.py down
```

Certificate renewal keeps the private keys; generated revision labels make the
next `up` recreate consumers. Leaf certificates last 30 days and are renewed
automatically within seven days of expiry. `trust` explicitly installs the public
CA (macOS login keychain or Linux system store); Firefox may require a separate
browser import. Never disable TLS verification. `down` preserves volumes.

The CLI enables the canonical `local-auth` and `document-compiler` Compose
profiles. A short-lived trusted job initializes a private RustFS bucket, scoped
credentials, versioning and seven-day output lifecycle rules. The compiler uses
the existing API/supervisor container and isolated, disposable worker processes.
Tectonic compiles in Rust; no external LaTeX executable is installed or launched.
The worker image initializes its public root filesystem and exits. Complete TeX
resources are downloaded once from the pinned official archive, checked against
its SHA-256, converted to a TTB bundle and then mounted read-only. Compilation
workers have no network or credentials. An empty template catalog is valid.
The pinned distribution is Tectonic `tlextras-2022.0r0`. Packages outside that
distribution must be supplied as authorized template assets. Updating the bundle
requires a reviewed source checksum and compatibility validation.

Compiler images are resolved to immutable registry digests. When deliberately
changing worker images or bundles, stop the stack and recreate only its public
`document-worker-root` volume before startup; the immutable installer rejects
in-place replacement. This volume contains public runtime resources, not PDFs,
templates or queue data. Keep persistent storage volumes when upgrading.

The CLI supervises commands with asyncio, timeouts and cancellation. Application
telemetry uses the existing OTLP collector. Set `GALADRIL_DEV_OTLP_ENDPOINT` to an
OTLP HTTP traces endpoint to export CLI operation spans; secrets, bodies and raw
exceptions are excluded. `doctor` verifies HTTPS with the generated CA and checks
the compiler's mandatory startup probes. It does not replace an authorized PDF
compilation test or a Kubernetes deployment test.

For production use the [Vault and certificate-manager overlay](../kubernetes-overlays/production/README.md).
