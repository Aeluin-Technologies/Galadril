# Proxy trust boundary

Every network-facing Galadril API is served by a colocated Envoy. Application
listeners bind to loopback in the proxy's network namespace. A Docker bridge,
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
and is excluded from published Services; Kubernetes NetworkPolicy restricts it
to node probes. The public route exposes only `/graphql`.

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
