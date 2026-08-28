#!/bin/bash
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE spicedb'
    WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'spicedb')
    \gexec
EOSQL
