# Operating the API proxies

Docker Compose is the current deployment. `infrastructure/envoy` contains its
Envoy v3 bootstrap files. Compose places each application in its proxy's network
namespace. Application bindings are fixed to loopback; the only published API
port is Gateway's TLS listener on 8080. Kubernetes is a future deployment target,
with Istio Ambient as the intended mesh architecture.

`127.0.0.1` is the shared application/proxy loopback, not the Docker host or a
remote Pod. Compose uses `network_mode: service:<application>-proxy`; using this
loopback profile on Kubernetes requires both containers in the same Pod.
Container colocation on the same
machine or attachment to the same Docker bridge is insufficient. Each caller
reaches its own local proxy; that proxy resolves the remote service's DNS name.

| API connection | Listener | Authentication |
| --- | --- | --- |
| Client to Gateway | `gateway-proxy:8080/graphql` | TLS and JWT |
| Gateway to Registry | local 50052 → `registry-proxy:50052` | Workload mTLS |
| Intake/Vision to Registry | local 50052 → `registry-proxy:50052` | Workload mTLS, read RPC allowlist |
| Gateway to Scribe | local 8092 → `scribe-proxy:8443/runs` | Workload mTLS |
| Scribe to Gateway tools | local 8082 → `gateway-proxy:8443/internal/chat/tools` | Workload mTLS and generation capability |

Applications use local plaintext only within their shared network namespace;
every API connection crossing a namespace uses TLS 1.3. Reserve that namespace
for the application and its proxy. Each proxy receives only its own certificate,
private key and trust bundle. Never mount the CA signing key or another workload's
private key. URI SANs are `spiffe://galadril/{gateway,registry,intake,vision,scribe}`;
issuing these identities requires a trusted deployment controller.

For development, create disposable certificates with the native OpenSSL CLI:

```sh
bash infrastructure/envoy/dev-certificates.sh .local/identity
```

Supply `.local/jwks.json` from the trusted identity provider. Set the issuer and
audience in `infrastructure/envoy/gateway.yaml` to match that provider. The JWKS
contains public signing keys only; JWT issuance belongs to the identity provider.
Development certificates last seven days. Browsers and API clients must explicitly
trust the development CA; do not disable certificate verification.

```sh
GALADRIL_PROXY_UID="$(id -u)" GALADRIL_PROXY_GID="$(id -g)" \
  docker compose -f infrastructure/docker/docker-compose.yaml up -d
```

Production supplies `GALADRIL_IDENTITY_DIR` with separate workload directories,
`GALADRIL_PUBLIC_TLS_DIR` with the Gateway's publicly trusted certificate/key, and
`GALADRIL_JWKS_PATH` with the provider's public keys. Private keys must be readable
by the configured proxy UID/GID and inaccessible to other workloads. Compose's
defaults refer to untracked local files and fail to start without them. Enable
Scribe with `SCRIBE_ENABLED=true` and configure its model provider separately.

Gateway no longer accepts `JWT_*`, `PUBLIC_KEY_PEM` or `PRIVATE_KEY_PEM` as
authentication configuration. Scribe no longer accepts `SCRIBE_SERVICE_TOKEN`.
Shared connector files use `registry.endpoint: http://127.0.0.1:50052` and Gateway
binds to `127.0.0.1:8081`. Registry binds to `127.0.0.1:50053`.

## Existing Kubernetes reference

`infrastructure/kubernetes/registry.yaml` is an isolated Registry sidecar
reference. It requires the `registry-envoy` ConfigMap and
`registry-workload-identity` Secret; it does not constitute a complete deployment
and does not install the future Ambient mesh. Its Service targets Envoy, and its
NetworkPolicy admits named API callers when enforced by the CNI.

That reference does not install Envoy into Gateway, Intake or Vision Pods.
Adapting the Compose loopback configuration to such Pods would require their own
sidecars and certificates. Do not use `http://127.0.0.1:50052` for an ordinary
remote caller or confuse it with the Registry's private application port. The
planned Ambient deployment below will use a separate configuration profile.

## Shared proxies on Kubernetes nodes

[Istio Ambient](https://istio.io/latest/docs/ambient/overview/) provides a mesh
without sidecars: a `ztunnel` DaemonSet supplies workload mTLS on each node, while
shared Envoy waypoints provide L7 processing where needed. For Galadril, JWT
verification and RPC allowlists require that L7 layer. The public ingress still
terminates external TLS. Waypoints can serve multiple workloads and scale
independently instead of adding Envoy to every application replica.

Ambient is the intended Kubernetes migration direction. Applications would use
Service DNS rather than the current local egress ports, and the mesh would
intercept traffic transparently. Merely moving Envoy into a DaemonSet leaves
loopback unreachable and permits bypass unless interception and authorization
are configured. Preserve distinct workload identities, mandatory JWT validation,
RPC permissions, health checks and OTLP export throughout the migration. See
the [trust contract](../architecture/zero_trust.md#kubernetes-without-sidecars)
for enforcement requirements. No Ambient resources are deployed by the current
Registry manifest.

## Rotation and verification

Rotate certificates before expiry and restart proxies after replacing static
certificates, trust bundles or JWKS. Use overlapping old/new public keys and trust
roots during rollout. Managed deployments should use Envoy's native SDS/xDS and a
PKI controller for automatic rotation; never build a second JWT verifier in the
application. Registry remains at one replica until distributed branch locking
supports concurrent writers.

Envoy exports metrics, traces and access logs through the existing OTLP collector. Logs
include method, response code, response flags and duration; credentials, claims,
query strings and payloads are excluded. Envoy's admin port 9901 binds to loopback.
The dedicated health listener 9902 returns readiness based on active application
checks and exposes no API route. Circuit breakers bound pending requests and
connections; unhealthy upstreams are not served through panic-mode routing.
The [native OTLP metrics sink](https://www.envoyproxy.io/docs/envoy/v1.39.2/api-v3/extensions/stat_sinks/open_telemetry/v3/open_telemetry.proto)
is marked functional with limited production burn time by Envoy; include its
export/drop counters in operational monitoring.

Run `bazel test //infrastructure/envoy/tests/...` for bootstrap validation and real
Envoy JWT/mTLS rejection tests. The integration fixture uses an echo upstream;
Registry policy tests retain the actual TLS and RBAC filters while substituting
the application protocol/health check. The pipeline lifecycle suite uses the
real application images behind all five proxies and verifies TLS in its clients.

See [the trust contract](../architecture/zero_trust.md) for tenant authorization,
image integrity and the database/broker/storage transport scope. The development
Compose dependency credentials and plaintext connector transports require
separate production hardening before claiming platform-wide zero trust.
