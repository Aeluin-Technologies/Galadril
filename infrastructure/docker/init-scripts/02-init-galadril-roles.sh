#!/bin/bash
set -euo pipefail

# Development-only static credentials; production provisions rotated secrets.
psql \
    --variable=ON_ERROR_STOP=1 \
    --variable=app_password="${GALADRIL_APP_PASSWORD:-galadril_app}" \
    --variable=maintenance_password="${GALADRIL_MAINTENANCE_PASSWORD:-galadril_maintenance}" \
    --variable=target_database="$POSTGRES_DB" \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" <<'EOSQL'
SELECT format(
    'CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'galadril_app'
) \gexec

ALTER ROLE galadril_app NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE :"target_database" TO galadril_app;
GRANT CREATE ON DATABASE :"target_database" TO galadril_app;
GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;
GRANT USAGE ON SCHEMA ag_catalog TO galadril_app;
GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO galadril_app;

SELECT format(
    'CREATE ROLE galadril_maintenance LOGIN NOINHERIT NOSUPERUSER BYPASSRLS PASSWORD %L',
    :'maintenance_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'galadril_maintenance'
) \gexec

ALTER ROLE galadril_maintenance NOINHERIT NOSUPERUSER BYPASSRLS;
GRANT CONNECT ON DATABASE :"target_database" TO galadril_maintenance;
GRANT USAGE, CREATE ON SCHEMA public TO galadril_maintenance;
EOSQL
