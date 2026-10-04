#!/bin/bash
# Creates the LiteLLM database/user on first Postgres init.
# LITELLM_DB_PASSWORD comes from the environment (see docker-compose.yml) -
# no secret is hardcoded here, unlike the old init-db.sql this replaces.
set -e

if [ -z "$LITELLM_DB_PASSWORD" ]; then
  echo "init-db.sh: LITELLM_DB_PASSWORD is not set, aborting" >&2
  exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE litellm;
    CREATE USER litellm WITH PASSWORD '${LITELLM_DB_PASSWORD}';
    GRANT ALL PRIVILEGES ON DATABASE litellm TO litellm;
EOSQL

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname litellm <<-EOSQL
    GRANT ALL ON SCHEMA public TO litellm;
    ALTER SCHEMA public OWNER TO litellm;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO litellm;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO litellm;
EOSQL
