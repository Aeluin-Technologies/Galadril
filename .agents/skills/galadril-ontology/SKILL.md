---
name: galadril-ontology
description: >-
  Use when changing Galadril Registry ontologies, inheritance, semantic
  revisions, pipeline-to-ontology bindings, ESKG graph structure, identity
  resolution or graph materialization. Not for unrelated database tuning.
---
# Registry and ESKG contract preservation

First read `docs/src/architecture/ontology.md` and
`docs/src/architecture/eskg.md`. Check implementations and tests before
concluding that a documented design is deployed.

1. Registry owns ontology definitions, pipeline DAGs, semantic validation,
   publication and immutable revision identifiers. Do not move that authority
   to Vision, Gateway, a caller-provided path or a mutable cache.
2. Preserve tenant-scoped resource identity, ownership, inheritance/interface
   DAG acyclicity, dependency closures and deterministic semantic diffs or
   merges. Conflicts must be explicit and storage unchanged on rejection.
3. Treat the `ontology_ref` as an indivisible tuple of tenant, ontology,
   immutable revision, resource and resource kind. Resolve its parts from
   the pinned Registry slice; reject missing, mixed or wrong-kind references.
4. Keep ESKG *structural kinds* (`Entity`, `Event`, `State`, etc.) separate from
   business classifications (`Person`, `Transaction`, etc.). Business types
   are not AGE vertex labels or authority scopes.
5. Validate relation vocabulary, direction, permitted endpoint kinds and
   same-tenant graph edges before mutation. Distinguish observed evidence,
   latent hypotheses, decisions and authoritative identity.
6. Never rewrite old evidence to a newer ontology revision. Any migration or
   replay of durable evidence needs its own reviewed, non-destructive plan
   and provenance preservation.
7. Pin test fixtures to explicit revisions and test invalid edge pairs,
   inheritance cycles, missing dependencies, concurrent merges and tenant
   mismatches. Keep deterministic tests under Bazel.

Report the semantic contract changed, incompatible inputs, migration needs,
rejected cases and tests. Do not assume all ESKG kinds are presently
materialized; the architecture document distinguishes planned from live roles.
