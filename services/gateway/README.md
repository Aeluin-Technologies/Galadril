# Galadril gateway 🌉

> *"A stranger could not match his will against the stone, nor make it obey him,
> unless he possessed the rightful authority."*

The gateway is Galadril's main API. It allows staff to access, edit, and delete
data and permissions, upload documents, and track their progress using GraphQL.

The API therefore uses multi-layered authentication:
1. Checking JWT signature\*;
2. Checking specific permissions on SpiceDB (ReBAC);
3. Verifying request context with Cedar (ABAC).

Only after these three permission checks, requested data is extracted via
PostgreSQL using RLS. Every access, update, or deletion is permanently recorded.
Changes to the ontology and pipelines are tracked in detail, supporting Git-like
operations such as branching, merging, and version history.

\* JWT signature will be deported to Envoy proxy later.
