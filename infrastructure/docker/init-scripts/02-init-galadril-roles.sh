#!/bin/bash
set -euo pipefail

# Development-only static credentials; production provisions rotated secrets.
psql \
    --variable=ON_ERROR_STOP=1 \
    --variable=target_database="$POSTGRES_DB" \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<'EOSQL'
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'galadril_maintenance') THEN
        CREATE ROLE galadril_maintenance
            LOGIN SUPERUSER PASSWORD 'galadril_maintenance';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'galadril_app') THEN
        CREATE ROLE galadril_app
            LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD 'galadril_app';
    END IF;
END
$roles$;

GRANT CONNECT ON DATABASE :"target_database" TO galadril_app;
GRANT USAGE ON SCHEMA public TO galadril_app;
ALTER DEFAULT PRIVILEGES FOR ROLE galadril_maintenance IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO galadril_app;
ALTER DEFAULT PRIVILEGES FOR ROLE galadril_maintenance IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO galadril_app;
EOSQL
