# Operating the local API proxy

Docker Compose runs one Envoy process named `api-proxy`. Gateway, Registry,
Intake, Vision and Scribe share its network namespace with
`network_mode: service:api-proxy`. Their application listeners bind to loopback;
only Envoy's public TLS/JWT listener is published. This keeps local development
lightweight without installing a mesh control plane or a proxy per service.

The local application namespace is one trust domain. Applications can reach each
other's loopback listeners, so Compose does not enforce cryptographic isolation
between compromised workloads. Use the [Istio Ambient deployment](kubernetes.md)
for workload identities, strict mTLS, and source-specific API policies. Ambient
uses node tunnels and a shared waypoint, which provide more than a single proxy.

| API connection | Envoy listener | Upstream |
| --- | --- | --- |
| Client to Gateway | TLS/JWT 8080 `/graphql` | loopback 8081 |
| Application to Registry | loopback 50054, bounded RPC allowlist | loopback 50053 |
| Gateway to Scribe | loopback 8092 `/runs` | loopback 8091 |
| Scribe to Gateway tools | loopback 8082 `/internal/chat/tools` | loopback 8081 |
| Administrative Registry client | mTLS 50052, certificate/RPC allowlist | loopback 50053 |

The Registry mTLS listener supports authorized administrative clients and E2E
fixture setup; Compose does not publish it by default. Internal listeners remove
caller-supplied identity headers. Gateway retains tenant, object and generation
capability authorization. Registry bootstrap uses `http://127.0.0.1:50054`;
application bindings remain Gateway 8081, Registry 50053 and Scribe 8091.
`127.0.0.1` refers to the shared container namespace, not the Docker host.

## Configure TLS and JWT

Use the [guided development CLI](../../../infrastructure/dev/README.md):

```sh
uv run infrastructure/dev/galadril.py up --trust
uv run infrastructure/dev/galadril.py tenant --email demo@galadril.test
```

Configuration, certificates and public JWKS are generated under `.local/dev/`.
The CLI creates unique EC signing keys, a development CA valid for 365 days and
leaf certificates valid for 30 days. It automatically renews leaves within seven
days of expiry and preserves private keys. Explicit renewal is also available:

```sh
uv run infrastructure/dev/galadril.py certificates
uv run infrastructure/dev/galadril.py up --no-pull
```

The public CA is installed only with explicit `trust` or `--trust`. Browser trust
may require importing `.local/dev/identity/ca.pem` separately. Never disable TLS
verification. The CA signing key and local JWT signer stay outside container
mounts. Generated revision labels recreate certificate consumers on startup.

Envoy accepts verified Hydra tokens for the normal dashboard flow. Generated
local development JWKS additionally support short-lived diagnostic tokens:

```sh
uv run infrastructure/dev/galadril.py token --subject EXISTING_USER --tenant EXISTING_TENANT
```

The token is written to a private file and expires after 15 minutes. Signing a
JWT establishes identity only: Gateway still checks tenant and resource grants
through SpiceDB/Cedar. Tenant enrollment uses Gateway's existing provisioning
API. No CLI-generated signer is deployed to Kubernetes. The standalone
`infrastructure/envoy/gateway.yaml` remains a proxy test fixture; production uses
Hydra's public JWKS and verified TLS.

Gateway no longer accepts `JWT_*`, `PUBLIC_KEY_PEM` or `PRIVATE_KEY_PEM` as
application authentication settings. Scribe no longer accepts
`SCRIBE_SERVICE_TOKEN`. Shared Compose networking remains a local trust domain;
use the Ambient deployment for workload separation.

## Compose and E2E parity

The pipeline E2E services inherit the shipped Compose definitions using native
`extends`, including the single proxy image and bootstrap arguments. Fixture
overrides select current application builds, isolated volumes, localhost ports,
deterministic models and telemetry sampling. The E2E Registry administrative
port remains protected by mTLS; application Registry calls use the local proxy
listener. Public authentication and all normal internal API paths use Envoy.

Separate daemon-owned volumes keep server keys outside application, database and
observability mounts. The proxy receives its configuration and server keys in
one read-only volume, avoiding overlapping mounts under a read-only parent.
The native merged Compose model verifies one proxy, shared namespaces, inherited
hardening and absence of published application ports. PostgreSQL preserves its
extension preloads and waits for its final TCP server. MinIO flushes individual
Kafka notifications, Vision uses the native gRPC resolver, and Gateway waits for
the canonical Zed schema job. E2E-only Ray settings remain fixtures.

The E2E fixture does not require volume subpath support; `!override` requires
Compose 2.24.4+. CI and E2E use the official Compose 5.6.0 binary supplied by
`//infrastructure/docker:compose`, pinned per platform with SHA-256 checksums.
This keeps schema bootstrap dependencies independent of the executor's installed
Compose version. The merged-model regression checks every inherited application
dependency, including Gateway's successful SpiceDB schema installation. Use
Compose 5.6.0 for the same deployment behavior locally. Registry remains at one
replica until distributed branch locking supports concurrent writers.

## Health, telemetry and rotation

Envoy checks Gateway/Scribe HTTP health and Registry's gRPC health, rejects
unhealthy upstreams, and bounds connections and pending requests. Administration
and health listeners bind to shared loopback. Logs, traces and metrics use OTLP
without credentials, bodies or query strings. Keep the OTLP collector available.

Rotate certificates before expiry and restart Envoy after replacing its static
certificates, trust bundles or JWKS. Use overlapping public keys/trust roots when
rotating. Ambient manages workload certificate rotation separately; its public
TLS Secret and JWKS still require their own lifecycle.

Run `bazel test //infrastructure/docker/tests/... //infrastructure/envoy/tests/...`
for native Compose contracts and actual Envoy validation. The single-proxy test
covers public JWT/header integrity and the Registry, generation and tools paths.
`//tests/e2e:pipeline_lifecycle_test` exercises the application pipeline with this
same proxy; `//infrastructure/kubernetes:ambient_test` exercises actual mesh
policies with echo API fixtures. See the [deployment verification matrix](kubernetes.md#deployment-verification)
for the scope and limitations of each suite.
