# Galadril Database

This repository contains the Docker building for the Galadril database engine.
It is a specialized distribution of PostgreSQL optimized for time-series, vector and graph processing.

| Component | Functionality |
| :--- | :--- |
| **PostgreSQL** | Core relational database engine |
| **TimescaleDB** | Time-series data with vector support |
| **Apache AGE** | Graph database and Cypher query support |

The image initializes required extensions and provisions `galadril_app` as a
`NOSUPERUSER NOBYPASSRLS` login with `CREATE` on the `public` application
schema. Gateway and Vision therefore initialize their own idempotent tables and
security objects without running as PostgreSQL superusers.

The image also provisions `galadril_maintenance` as a non-superuser maintenance
identity. It can bypass RLS only for the explicitly granted Vision outbox work;
it receives no general tenant-table privileges.
