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

Adapt `examples/connectors.ambient.yaml` to the dependency Services or managed
endpoints. Its sample dependency credentials and plaintext transports are only
illustrative. Keep `gateway.proxy_mode: ambient`, `gateway.host: 0.0.0.0`,
`registry.endpoint: http://registry.galadril.svc.cluster.local:50053`, and
`scribe.endpoint: http://scribe.galadril.svc.cluster.local:8091`. These HTTP URLs
are intercepted and encrypted by Ambient. Select the tenant and pipeline in
Vision's environment for the deployment being operated.

Create the namespace first, then provide these Secrets through the deployment's
secret manager or `kubectl create secret`:

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

Supply the identity provider's **public** JWKS in
`infrastructure/kubernetes/jwks.json`. The checked-in empty key set denies every
JWT. Kustomize projects this single input into both Istio's
`RequestAuthentication` and the native Envoy JWT provider. If changing issuer,
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
# Create the Secrets and configure trusted public keys and release images.
kubectl apply -k infrastructure/kubernetes
kubectl wait -n galadril --for=condition=complete job/spicedb-schema --timeout=180s
kubectl wait -n galadril --for=condition=Programmed gateways.gateway.networking.k8s.io --all --timeout=180s
kubectl rollout status -n galadril deployment/gateway
```

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
