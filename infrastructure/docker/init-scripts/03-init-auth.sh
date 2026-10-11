#!/usr/bin/env bash
# Separate signing and federation credentials from tenant application access.
set -euo pipefail
psql --variable=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --variable=auth_password="$GALADRIL_AUTH_PASSWORD" \
  --variable=hydra_password="$HYDRA_DATABASE_PASSWORD" \
  --variable=polis_password="$POLIS_DATABASE_PASSWORD" \
  --variable=kratos_password="${KRATOS_DATABASE_PASSWORD:-}" <<'SQL'
SELECT format('CREATE ROLE galadril_auth LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'auth_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='galadril_auth') \gexec
GRANT USAGE ON SCHEMA public TO galadril_auth;
SELECT format('CREATE ROLE hydra LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'hydra_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='hydra') \gexec
SELECT format('CREATE ROLE polis LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'polis_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname='polis') \gexec
SELECT 'CREATE DATABASE hydra OWNER hydra' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='hydra') \gexec
SELECT 'CREATE DATABASE polis OWNER polis' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname='polis') \gexec
SELECT format('CREATE ROLE kratos LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD %L', :'kratos_password')
WHERE :'kratos_password' <> '' AND NOT EXISTS (SELECT FROM pg_roles WHERE rolname='kratos') \gexec
SELECT 'CREATE DATABASE kratos OWNER kratos'
WHERE :'kratos_password' <> '' AND NOT EXISTS (SELECT FROM pg_database WHERE datname='kratos') \gexec
SQL
