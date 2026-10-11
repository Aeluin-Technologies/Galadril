# Kubernetes and k3s with Istio Ambient

Docker Compose uses one shared local Envoy and remains the lighter deployment
option. Its application namespace is one trust domain. Kubernetes runs the five
application workloads without Envoy sidecars. Istio installs one ztunnel per
node, one shared `api-waypoint` for HTTP/gRPC policy, and a public ingress that
terminates TLS and validates JWTs. Applications use Service DNS and explicitly
select `ambient` proxy mode; the default mode still requires loopback.

The manifests use native Kustomize and the upstream Istio Helm charts. The
validated versions are Istio 1.31.1, Gateway API 1.6.3, and k3s 1.37.1. Use a
Kubernetes release in [Istio's supported range](https://istio.io/latest/docs/releases/supported-releases/).
K3s runs on Linux; on macOS, k3d runs it inside Docker's Linux VM. Adding Istio
also adds its control plane, CNI agent, node tunnel and shared proxies. Compose
therefore remains the smaller option for a Mac. Vision's embedded Ray runtime
has a separate memory requirement; replacing Kubernetes with k3s does not
reduce the application's model or worker memory.

## Install the mesh

Use the desired Kubernetes context. On k3s, disable its bundled Traefik when
installing the server (`--disable=traefik`). For an isolated local k3d cluster:

```sh
k3d cluster create galadril --image rancher/k3s:v1.37.1-k3s1 \
  --k3s-arg '--disable=traefik@server:*'
```

Install [the official Ambient components](https://istio.io/latest/docs/ambient/install/helm/).
Select `k3s.yaml` for k3s, `k3d.yaml` for k3d, or omit that values file on a
standard Kubernetes cluster with the usual CNI paths:

```sh
kubectl apply --server-side --force-conflicts -f https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.3/standard-install.yaml
helm repo add istio https://blob.istio.io/istio-release/charts
helm repo update
helm upgrade --install istio-base istio/base -n istio-system --create-namespace --version 1.31.1 --wait
helm upgrade --install istiod istio/istiod -n istio-system --version 1.31.1 \
  -f infrastructure/kubernetes/istio/istiod.yaml --wait
helm upgrade --install istio-cni istio/cni -n istio-system --version 1.31.1 \
  -f infrastructure/kubernetes/istio/cni.yaml \
  -f infrastructure/kubernetes/istio/k3s.yaml --wait
helm upgrade --install ztunnel istio/ztunnel -n istio-system --version 1.31.1 \
  -f infrastructure/kubernetes/istio/k3s.yaml --wait
```

The k3s/k3d values explicitly select `/var/lib/rancher/k3s/data/cni`, the CNI
binary directory used by the validated k3s version. Check the platform's paths
when upgrading. The namespace enforces Kubernetes restricted Pod security;
`proxy-defaults` supplies seccomp to the generated ingress and waypoint.

## Supply application configuration

The application deployment expects PostgreSQL with the Galadril extensions,
Kafka and its schema registry, S3, lakeFS, SpiceDB, an OTLP collector and a model
provider if Scribe is enabled. These dependencies remain separately operated
services; the manifests do not provision their databases, storage or credentials.
Use TLS and scoped accounts for those connectors. Ambient protects enrolled
workloads, not an arbitrary external endpoint.

Adapt `infrastructure/kubernetes/connectors.yaml` to the dependency Services or managed
endpoints. Its sample dependency credentials and plaintext transports are only
illustrative. Keep `gateway.proxy_mode: ambient`, `gateway.host: 0.0.0.0`,
`registry.endpoint: http://registry.galadril.svc.cluster.local:50053`, and
`scribe.endpoint: http://scribe.galadril.svc.cluster.local:8091`. These HTTP URLs
are intercepted and encrypted by Ambient. Select the tenant and pipeline in
Vision's environment for the deployment being operated.

Create the namespace first, then provision these Secrets with
[Vault Secrets Operator](#vault-backed-secrets):

| Secret | Required content |
| --- | --- |
| `gateway-connectors`, `registry-connectors`, `intake-connectors`, `vision-connectors` | Each workload's `connectors.yaml`, with only its required credentials |
| `gateway-runtime` | `DATABASE_URL`; optional Gateway configuration overrides |
| `registry-runtime` | `LAKEFS_ENDPOINT`, `LAKEFS_ACCESS_KEY_ID`, `LAKEFS_SECRET_ACCESS_KEY` |
| `scribe-runtime` | `SCRIBE_MODELS`, `SCRIBE_DEFAULT_MODEL` and model provider credentials |
| `spicedb-bootstrap` | `SPICEDB_ENDPOINT`, `SPICEDB_TOKEN` for the canonical schema job and Gateway's startup check; the endpoint must support native TLS |
| `galadril-public-tls` | Kubernetes TLS Secret containing the public ingress certificate and key |

Scribe receives model settings and scoped generation capabilities, without the
shared connector Secrets. Its tools URL resolves Gateway through the mesh.
The schema Job installs `schemas/spicedb/schema.zed`; Gateway verifies that
schema at startup. On a schema upgrade, delete the completed `spicedb-schema`
Job before applying the new manifests, because Kubernetes Job templates are
immutable. Schema compatibility remains enforced by SpiceDB and Gateway.

Configure the free self-hosted [Polis/Hydra SSO stack](enterprise_sso.md).
Ingress retrieves Hydra's **public** JWKS dynamically through authenticated mesh
transport. The old `jwks.json` file is no longer consumed by Kustomize.
Install the shipped Istiod values, including remote Envoy JWKS fetching.
If changing issuer,
audience or projected claims, update both declarations in `ingress.yaml` and run
the contract and mesh tests. The `origins-0` provider and `payload` metadata key
match the pinned Istio version; verify them before an Istio upgrade. The complete
provider is required because EnvoyFilter's map merge replaces that provider.

Use a Kustomize release overlay to pin each application image by its verified
digest. The base image tags are development defaults. Restrict who may mutate
namespace labels, workload ServiceAccounts, mesh policies and public keys;
enforce signed image admission in production.

```sh
kubectl apply -f infrastructure/kubernetes/namespace.yaml
# Create the Secrets and configure the SSO issuer and release images.
kubectl apply -k infrastructure/kubernetes
kubectl wait -n galadril --for=condition=complete job/spicedb-schema --timeout=180s
kubectl wait -n galadril --for=condition=Programmed gateways.gateway.networking.k8s.io --all --timeout=180s
kubectl rollout status -n galadril deployment/gateway
```

## Vault-backed secrets

Use an operated Vault deployment with TLS, persistent storage, unseal/recovery
procedures and audit logging. The application manifests do not install or
initialize Vault. Install the official
[Vault Secrets Operator](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/vso/installation)
with a reviewed, pinned Helm chart version. Configure its telemetry and audit
collection with the cluster's OTLP pipeline.

`infrastructure/kubernetes/vault` supplies a separate Kustomize bundle for the
operator CRDs. It uses short-lived Kubernetes authentication through the
dedicated `vault-secrets` ServiceAccount, KV v2 synchronization for the eight
application Secrets above, and PKI issuance for `galadril-public-tls`. The
operator renews the ingress certificate one hour before its 24-hour expiry.
Istio watches the TLS Secret; application credential changes restart their
owning Deployments because environment values and startup configuration are
loaded once. Re-run the immutable schema Job explicitly for schema changes.

Configure the existing Vault Kubernetes auth mount to validate this cluster's
ServiceAccount tokens. Vault needs TokenReview access on the Kubernetes API;
follow the [upstream Kubernetes auth setup](https://developer.hashicorp.com/vault/docs/auth/kubernetes).
Then create a scoped policy and role from an authenticated operator terminal:

```sh
vault policy write galadril-secrets - <<'HCL'
path "kv/data/galadril/*" {
  capabilities = ["read"]
}
path "pki_int/issue/galadril-public" {
  capabilities = ["update"]
}
HCL
vault write auth/kubernetes/role/galadril-secrets \
  bound_service_account_names=vault-secrets \
  bound_service_account_namespaces=galadril \
  audience=vault policies=galadril-secrets ttl=10m
```

Provision `kv` as a KV v2 mount and `pki_int` with a signed intermediate CA using
the Vault operator's existing PKI process. Create its narrowly scoped leaf role:

```sh
vault write pki_int/roles/galadril-public \
  allowed_domains=api.galadril.example allow_bare_domains=true \
  allow_subdomains=false allow_ip_sans=false \
  server_flag=true client_flag=false key_type=ec key_bits=256 max_ttl=24h
```

Replace the example hostname in this role and `vault/secrets.yaml`, and set
`VaultConnection.spec.address` to the deployment's TLS Vault endpoint. Supply
its public CA chain in the `vault-ca` Secret. Populate each application KV path
with the same key names expected by the Secret table, using trusted files:

```sh
vault kv put -mount=kv galadril/gateway-connectors \
  connectors.yaml=@/secure/gateway-connectors.yaml
vault kv put -mount=kv galadril/gateway-runtime @/secure/gateway-runtime.json
```

Repeat for the remaining six Secret names, keeping each workload's
credentials separate. The operator's policy can read these paths; application
ServiceAccounts receive neither Vault credentials nor Vault access. No JWT
signing key or CA signing key is synchronized into application Secrets. Native
Kubernetes Secrets require encryption at rest and namespace RBAC as part of the
cluster's configuration.

Apply the secret bundle before deploying applications:

```sh
kubectl apply -f infrastructure/kubernetes/namespace.yaml
kubectl create secret generic vault-ca -n galadril \
  --from-file=ca.crt=/secure/vault-ca.pem
kubectl apply -k infrastructure/kubernetes/vault
kubectl wait -n galadril --for=condition=Ready \
  vaultstaticsecrets.secrets.hashicorp.com --all --timeout=180s
kubectl wait -n galadril --for=condition=Ready \
  vaultpkisecrets.secrets.hashicorp.com/galadril-public-tls --timeout=180s
kubectl apply -k infrastructure/kubernetes
```

The public JWT issuer remains a separate identity authority. Vault can provide
that authority through its
[OIDC provider](https://developer.hashicorp.com/vault/docs/concepts/oidc-provider).
Configure its clients, user authentication and tenant claims for the deployment,
then publish only its public JWKS through the Kustomize input described above.
Update both ingress issuer/audience declarations together. JWKS publication is a
release input, so refresh and apply it on signing-key rotation with overlapping
keys. Do not store an exported production JWT signing key in KV or reuse the
local example signer. Istio continues to manage Ambient workload certificates.

The public LoadBalancer Service exposes HTTPS on 443. For local access:

```sh
kubectl port-forward -n galadril service/public-gateway-istio 8443:443
```

## Enforcement and observability

Ingress exposes only `/graphql`, removes caller-supplied identity headers, and
uses Envoy's native JWT verification with mandatory expiration and zero clock
skew. Missing or rejected identity returns 401 or 403 at ingress. Gateway retains
tenant, resource and generation-capability authorization. TLS 1.3 is required
on ingress and mesh connections.

Namespace enrollment uses a waypoint for both Service and Pod destinations.
The waypoint policy targets its Gateway and matches destination port, source
ServiceAccount, method and exact API path, including the Registry RPC allowlists.
This also protects direct Pod IP calls. Destination ztunnel policies accept
only the waypoint's identity; `PeerAuthentication` requires strict mTLS.
The original caller is checked at the waypoint, before that final hop.
Node readiness probes are a Kubernetes administrative path, not public routes.

Application telemetry uses OTLP. Istio's tracing and access logs use the `otlp`
and `otlp-logs` providers configured for
`otel-collector.observability.svc.cluster.local:4317`. Ensure the collector exists
and enroll its namespace or secure its transport. Collect native Istio/ztunnel
Prometheus metrics through the OpenTelemetry Collector's Prometheus receiver,
then export through the existing metrics pipeline. Do not include credentials,
query strings or bodies in access log formats. Workload certificates rotate
through Istio; the public TLS Secret and public JWKS have separate rotation.

## Deployment verification

| Suite | Deployment exercised | Scope |
| --- | --- | --- |
| `//infrastructure/docker/tests:test_models` | Native merged production/E2E Compose | Shared proxy settings, isolated keys, schema dependency, no app port publishing |
| `//infrastructure/docker/tests:collectors_test` | Shipped Alloy and OpenTelemetry Collector images | Native validation of profiling and OTLP configurations |
| `//database/tests:extensions_test` | Host-platform database image built by Bazel | Functional operations for every initialized PostgreSQL extension |
| `//database/tests:image_test` | Linux AMD64 and ARM64 database OCI index | Executable platforms, SBOMs, and provenance attestations |
| `//infrastructure/envoy/tests:proxy_test` | Actual Envoy image and shipped bootstraps | TLS/JWT/mTLS and RPC denials with echo fixtures |
| `//tests/e2e:pipeline_lifecycle_test` | Actual application images, inherited Compose services | Pipeline, chatbot, tenant authorization, telemetry through the proxies |
| `//infrastructure/kubernetes:ambient_test` | Actual k3s, Istio ingress, waypoint and ztunnel | JWT/header integrity, TLS version, plaintext denial, workload RPC permissions, direct Pod access |

The mesh suite substitutes small echo API fixtures and their HTTP protocol; it
does not prove the complete application pipeline on Kubernetes. The application
lifecycle suite remains Compose-backed. Preserve that distinction when reporting
CI results. Kubernetes dependencies, image releases and model resources require
deployment-specific acceptance tests.

All suites run through the shared Bazel CI. With Docker running locally, the
Ambient target creates an isolated k3s cluster and installs its pinned Istio
components using tools and charts fetched and verified by Bazel:

```sh
bazel test //infrastructure/kubernetes:ambient_test --test_output=errors
```

The target owns a unique cluster name, kubeconfig, Helm state, and dynamically
allocated ingress port. It deletes the cluster after success, failure, or Bazel
cancellation. It is included in `bazel test //...`; no preconfigured Kubernetes
context is required. Docker tests use the existing BuildBuddy Firecracker
execution profile in CI and the local Docker engine on macOS.
