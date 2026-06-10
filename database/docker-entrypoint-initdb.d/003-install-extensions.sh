#!/bin/bash
set -e

create_sql=$(mktemp)
create_sql_postgres_only=$(mktemp)

TARGET_DB="${POSTGRES_DB:-postgres}"

cat <<EOF >${create_sql}
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS vector CASCADE;
CREATE EXTENSION IF NOT EXISTS vectorscale CASCADE;
CREATE EXTENSION IF NOT EXISTS age CASCADE;
CREATE EXTENSION IF NOT EXISTS postgis CASCADE;
CREATE EXTENSION IF NOT EXISTS plpython3u CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_wait_sampling CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_repack CASCADE;
CREATE EXTENSION IF NOT EXISTS pg_trgm CASCADE;
EOF

# pg_cron has a strict requirement: it can ONLY be installed in the database
# defined by `cron.database_name` in the server config.
cat <<EOF >${create_sql_postgres_only}
CREATE EXTENSION IF NOT EXISTS pg_cron;
EOF

export PGPASSWORD="${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}"

psql -U "${POSTGRES_USER}" -d postgres -f "${create_sql_postgres_only}"
psql -U "${POSTGRES_USER}" -d "${TARGET_DB}" -f "${create_sql}"

if [ "${TARGET_DB}" != "postgres" ]; then
    echo "Initializing extensions in default 'postgres' database..."
    psql -U "${POSTGRES_USER}" -d postgres -f "${create_sql}"
fi

rm -f "${create_sql}" "${create_sql_postgres_only}"
