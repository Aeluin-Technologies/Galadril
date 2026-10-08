# Proxy trust boundary

The current Docker Compose deployment serves every network-facing Galadril API
through a colocated Envoy. Application listeners bind to loopback in the proxy's
network namespace. Colocation enforces isolation in Compose. The Ambient deployment uses
transparent interception and a required shared waypoint. A Docker bridge,
cluster network, source IP, or tenant identifier is never proof of identity.

The public Gateway listener requires TLS and a JWT with a valid signature,
issuer, audience, expiry and not-before time. Envoy removes incoming identity
headers before validation and projects verified claims into reserved headers.
Gateway accepts exactly one value for each mandatory claim, validates tenant
syntax, and rejects expired identities. HTTP and WebSocket upgrades share this
boundary; WebSocket connections terminate at token expiry.

Internal API listeners require client certificates signed by the workload CA
and an explicitly permitted URI SAN. Clients validate both the server's chain
and its exact workload URI SAN. TLS is required in both directions. Registry
readers cannot call mutation RPCs. Gateway can mutate Registry state; Scribe
can call only the delegated tool endpoint, and only Gateway can start Scribe
runs. The delegated generation capability remains a resource authorization
credential and expires independently of workload authentication.

An authenticated workload does not acquire tenant or object permissions.
Gateway retains SpiceDB, Cedar, PostgreSQL RLS and audit checks. Registry trusts
Gateway's authorized mutation requests; Intake and Vision receive read-only
runtime access. Compromise of a workload grants at most its explicitly allowed
transport routes. Protect pod execution, deployment configuration, certificate
issuance and secret storage as part of the trusted computing base.

Readiness reflects the application listener, including successful startup of
its required dependencies. Envoy checks Gateway/Scribe health and Registry's
gRPC health service, rejects unhealthy upstreams, bounds connections, and
exports traces and access decisions over OTLP without credentials or payloads.
Envoy administration is local. The dedicated health port exposes no API route
and is excluded from published Services. Kubernetes uses native application
probes and keeps health routes out of the public ingress. The public route exposes only `/graphql`.

Certificate chain and SAN verification establish transport identity and message
integrity. They do not attest the running binary or host. Signed image admission,
image digests, trusted nodes and, when required, hardware-backed attestation must
be enforced by the orchestrator. Static bootstrap files require an explicit
proxy restart for JWKS/trust changes; production PKI rotation must reload them
before expiry or replace static secrets with SDS.

This boundary covers Gateway, Registry, Scribe and their API callers. Kafka,
PostgreSQL, S3, lakeFS, SpiceDB, model providers and OTLP have their own connector
credentials and transport configuration. Their sample Compose transports remain
development-only; a production rollout also requires TLS and narrowly scoped
accounts for each of those dependencies.

## Kubernetes without sidecars

The Kubernetes and k3s deployment uses a managed mesh without application
sidecars. [Istio Ambient](https://istio.io/latest/docs/ambient/overview/) separates
the node-level mTLS tunnel (`ztunnel`) from shared Envoy waypoint proxies that
enforce HTTP/gRPC policies. Workload identity remains distinct per service;
sharing a proxy must not replace it with a common node certificate or source IP.
JWT validation, trusted claim projection and RPC allowlists require the L7
waypoint or ingress layer. The L4 tunnel alone cannot enforce those policies.

The Ambient profile replaces loopback egress addresses with Service DNS, makes
application listeners reachable through the mesh, and enforces strict mTLS and
the required waypoint on every protected path. Direct Pod IP access and ingress
traffic must not bypass JWT or RPC checks. Gateway may trust projected identity
headers only when the mesh authenticates and restricts their supplying proxy.
The source-workload policy belongs at the waypoint; destination tunnel policy
must admit that waypoint, whose identity replaces the original caller on the
final hop. Separate ServiceAccounts preserve each service's authority.

Docker Compose remains the local deployment option. The native Kubernetes
manifests deploy the application workloads without sidecars and use upstream
Istio components. The dedicated mesh E2E suite verifies actual ingress,
waypoint and ztunnel enforcement with API echo fixtures; the full application
lifecycle E2E suite runs through the Compose proxies. See the
[Kubernetes deployment guide](../operations/kubernetes.md) for dependencies,
configuration, telemetry and the exact validation scope.
