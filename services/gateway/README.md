# Galadril gateway 🌉

> *"A stranger could not match his will against the stone, nor make it obey him,
> unless he possessed the rightful authority."*

The gateway is Galadril's main API. It allows staff to access, edit, and delete
data and permissions, upload documents, and track their progress using GraphQL.

The API therefore uses multi-layered authentication:
1. Envoy validates JWT signatures, issuer, audience, and expiration over TLS;
2. Checking specific permissions on SpiceDB (ReBAC);
3. Verifying request context with Cedar (ABAC).

Only after these three permission checks, requested data is extracted via
PostgreSQL using RLS. Every access, update, or deletion is permanently recorded.
Changes to the ontology and pipelines are tracked in detail, supporting Git-like
operations such as branching, merging, and version history.

The binary listens only on loopback beside Envoy and consumes verified claim
headers for both HTTP and WebSocket upgrades. It holds no JWT signing keys.
Internal Registry and Scribe calls use local egress listeners with workload mTLS.
See [proxy operation](../../docs/src/operations/proxies.md) for provisioning,
rotation, readiness, and the remaining connector transport requirements.

The library uses `pengoloLibraryChanges(projectId)` on the existing authenticated
GraphQL WebSocket. A PostgreSQL trigger emits scope-only invalidations after
committed file changes, including indexing progress and publications from other
Gateway replicas. Each Database owns one additional LISTEN connection, separate
from the transaction pool. Disconnects and lagged receivers cause a fresh
snapshot; notifications are hints, not durable file data.

Gateway filters tenant and personal/project scope, rechecks live permissions
before each invalidation, and rechecks quiet subscriptions every 30 seconds.
The browser coalesces notifications, reloads its displayed cursor pages through
the normal authorized API, and releases the subscription when the view is
hidden or deactivated. There is no periodic library request in the browser.
