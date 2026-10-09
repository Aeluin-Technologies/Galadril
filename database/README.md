# Galadril Database

This repository contains the Docker building for the Galadril database engine.
It is a specialized distribution of PostgreSQL optimized for time-series, vector
and graph processing.

| Component | Functionality |
| :--- | :--- |
| **PostgreSQL** | Core relational database engine |
| **TimescaleDB** | Time-series data with vector support |
| **Apache AGE** | Graph database and Cypher query support |
| **pgvector** | Vector embeddings and similarity search |
| **PostGIS** | Spatial and geographic data management |

The image initializes required extensions and provisions `galadril_app` as a
`NOSUPERUSER NOBYPASSRLS` login with `CREATE` on the `public` application
schema. Gateway and Vision therefore initialize their own idempotent tables and
security objects without running as PostgreSQL superusers.

The image also provisions `galadril_maintenance` as a non-superuser maintenance
identity. It can bypass RLS only for the explicitly granted Vision outbox work;
it receives no general tenant-table privileges.

## Build and verification

The Bazel CI owns the database build, extension verification, and publication.
Docker must be running locally (Docker Desktop or OrbStack on macOS).

```sh
bazel test //database/tests:extensions_test
bazel test //database/tests:image_test
bazel build //database:image
bazel run //database:load
```

The extension test loads the host architecture from the Bazel build, initializes
both `postgres` and a dedicated application database, and executes TimescaleDB,
pgvector, pgvectorscale/DiskANN, AGE, PostGIS, PL/Python, pg_stat_statements,
pg_wait_sampling, pg_repack, pg_trgm, and pg_cron operations. Initialization fails
immediately if any extension cannot be created.

`//database:image` assembles Linux AMD64 and ARM64 images. BuildBuddy runs Docker
builds on x86_64 Firecracker workers, using BuildKit's bundled QEMU for ARM64.
The shared ARM64 executor pool does not support Docker-in-Firecracker. Local
builds use the host Docker engine and its emulation support.
The image test verifies both platforms and their SBOM and provenance attestations.
The image is included in `bazel run //:push`, which CI invokes on `main` after
`bazel test //...` succeeds. Building and testing never publish an image.
Database publication preserves the `latest` and Git commit SHA tags.
