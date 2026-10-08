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

Create disposable certificates using the native OpenSSL CLI:

```sh
bash infrastructure/envoy/dev-certificates.sh .local/identity
```

The shared proxy mounts Registry's server identity at `/etc/envoy/identity` and
Gateway's public certificate/key at `/etc/envoy/public`. Applications receive
neither directory. Never mount the CA signing key. Development certificates last
seven days; clients must trust the development CA explicitly.

Supply `.local/jwks.json` from the identity provider. Set the issuer and audience
in `infrastructure/envoy/local.yaml` to match that provider. The JWKS contains
public signing keys only; JWT issuance belongs to the identity provider. Envoy
requires a valid signature, issuer, audience, expiration and not-before time and
removes incoming identity headers before projecting verified claims. HTTP and
WebSocket upgrades use the same public authentication boundary.

```sh
GALADRIL_PROXY_UID="$(id -u)" GALADRIL_PROXY_GID="$(id -g)" \
  docker compose -f infrastructure/docker/docker-compose.yaml up -d
```

Override `GALADRIL_IDENTITY_DIR`, `GALADRIL_PUBLIC_TLS_DIR` and
`GALADRIL_JWKS_PATH` when using deployment-managed files. Keys must be readable by
the proxy UID/GID and inaccessible to application mounts. Enable Scribe with
`SCRIBE_ENABLED=true` and configure its model provider separately.

Gateway no longer accepts `JWT_*`, `PUBLIC_KEY_PEM` or `PRIVATE_KEY_PEM` as
authentication configuration. Scribe no longer accepts `SCRIBE_SERVICE_TOKEN`.
Local connector dependencies remain separately configured; sample database,
broker and object-storage transports are development defaults.

## Compose and E2E parity

The pipeline E2E services inherit the shipped Compose definitions using native
`extends`, including the single proxy image and bootstrap arguments. Fixture
overrides select current application builds, isolated volumes, localhost ports,
deterministic models and telemetry sampling. The E2E Registry administrative
port remains protected by mTLS; application Registry calls use the local proxy
listener. Public authentication and all normal internal API paths use Envoy.

Volume subpaths keep server keys outside application and observability mounts.
The native merged Compose model verifies one proxy, shared namespaces, inherited
hardening and absence of published application ports. PostgreSQL preserves its
extension preloads and waits for its final TCP server. MinIO flushes individual
Kafka notifications, Vision uses the native gRPC resolver, and Gateway waits for
the canonical Zed schema job. E2E-only Ray settings remain fixtures.

Volume subpaths require Docker Engine 26+ and Compose 2.35+; `!override` requires
Compose 2.24.4+. Registry remains at one replica until distributed branch locking
supports concurrent writers.

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
