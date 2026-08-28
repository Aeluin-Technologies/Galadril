#!/bin/bash
set -euo pipefail

# Development defaults are overridden by deployment-managed secrets.
app_password="${GALADRIL_APP_PASSWORD:-galadril_app}"
maintenance_password="${GALADRIL_MAINTENANCE_PASSWORD:-galadril_maintenance}"

psql \
    --variable=ON_ERROR_STOP=1 \
    --variable=app_password="${app_password}" \
    --variable=maintenance_password="${maintenance_password}" \
    --variable=target_database="${POSTGRES_DB:-postgres}" \
    --username="${POSTGRES_USER:-postgres}" \
    --dbname="${POSTGRES_DB:-postgres}" <<'EOSQL'
SELECT format(
    'CREATE ROLE galadril_app LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'galadril_app'
) \gexec

ALTER ROLE galadril_app NOSUPERUSER NOBYPASSRLS;
GRANT CONNECT ON DATABASE :"target_database" TO galadril_app;
GRANT USAGE, CREATE ON SCHEMA public TO galadril_app;

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
