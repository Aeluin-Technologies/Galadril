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

Create disposable certificates using the native OpenSSL 3 CLI. On a Nix-managed
host, enter `nix shell nixpkgs#openssl` first if `openssl version` reports LibreSSL:

```sh
bash infrastructure/envoy/dev-certificates.sh .local/identity
```

Run this before starting Compose. The proxy requires existing certificate, key,
CA files; missing sources fail at mount setup. If an earlier Compose
run created empty directories, remove only those empty placeholders before
generating certificates:

```sh
rmdir .local/identity/gateway .local/identity/registry .local/identity
rmdir .local/jwks.json
```

`rmdir` refuses nonempty directories. Preserve existing identities and provider
keys; use the path overrides below when they are managed elsewhere.

The shared proxy mounts Registry's server identity at `/etc/envoy/identity` and
Gateway's public certificate/key at `/etc/envoy/public`. Applications receive
neither directory. Never mount the CA signing key. The development CA lasts
365 days and leaf certificates last 30 days; clients must trust the development CA
explicitly. Renew before expiry without changing private keys or application data:

```sh
bash infrastructure/envoy/dev-certificates.sh .local/identity --renew
```

Renewal validates the full replacement set before updating files and preserves the
previous identity directory in a private sibling backup. Restart Envoy and Node TLS
consumers afterwards. If the browser pins the old CA certificate, import the renewed
CA into its trust store. Never disable certificate verification to restore sign-in.
An expired local CA can interrupt JWKS retrieval even while an access token remains
valid; authentication dependency outages return 503 and preserve the sealed session,
whereas invalid or revoked credentials remain definitive denials. Access tokens keep
the configured 15-minute lifetime.

Configure the free self-hosted [Polis/Hydra SSO stack](enterprise_sso.md).
The shared Compose proxy fetches Hydra's public JWKS over verified TLS and
checks its exact server identity. It no longer mounts a static JWKS file. Set
the issuer and audience consistently in Hydra, Studio and Envoy. Envoy
requires a valid signature, issuer, audience, expiration and not-before time and
removes incoming identity headers before projecting verified claims. HTTP and
WebSocket upgrades use the same public authentication boundary.

The generated keys use owner-only permissions. Set the proxy UID/GID to the file
owner when starting Compose; the default container UID 1000 may differ from the
host account. For repeated runs, save `GALADRIL_PROXY_UID` and
`GALADRIL_PROXY_GID` in `infrastructure/docker/.env`.

### Standalone proxy JWT fixture

From the repository root, initialize a unique signing key and public JWKS:

```sh
uv run infrastructure/envoy/tools/local_jwt.py init .local/jwt
```

The example uses [PyJWT](https://pyjwt.readthedocs.io/en/stable/) and
`cryptography`; uv installs its declared dependencies in an isolated script
environment. Initialization refuses to overwrite an existing directory. The
private signing key stays under `.local/jwt`, outside every container mount.
Only `jwks.json` is given to the standalone `infrastructure/envoy/gateway.yaml`
profile at `/etc/envoy/jwks.json`. This offline profile is retained for isolated
proxy tests. Compose and Kubernetes use Hydra and reject tokens from this
unrelated fixture key. Do not use this example as the dashboard login flow.

Issue a 15-minute fixture token for an explicit subject and tenant. The request
below targets a separately configured standalone proxy using that fixture JWKS:

```sh
umask 077
uv run infrastructure/envoy/tools/local_jwt.py token .local/jwt \
  --subject local-user --tenant tenant > .local/token.jwt
curl --cacert .local/identity/ca.pem \
  --header "Authorization: Bearer $(cat .local/token.jwt)" \
  --header 'Content-Type: application/json' \
  --data '{"query":"{ __typename }"}' https://localhost:8080/graphql
```

JWT verification establishes identity. Gateway still checks tenant and resource
permissions through SpiceDB/Cedar; choose a subject with the required grants.
Regenerate the token after expiry. Never deploy this local signer or copy its
private key into Kubernetes. Use [Vault-backed Kubernetes secrets](kubernetes.md#vault-backed-secrets)
for the deployed stack.

```sh
GALADRIL_PROXY_UID="$(id -u)" GALADRIL_PROXY_GID="$(id -g)" \
  bazel run //infrastructure/docker:compose -- \
    -f infrastructure/docker/docker-compose.yaml up -d
```

Override `GALADRIL_IDENTITY_DIR` and `GALADRIL_PUBLIC_TLS_DIR`
when using deployment-managed files. Keys must be readable by
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
