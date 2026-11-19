#!/bin/bash
# Runs once, on an empty data volume. Database-per-service with a dedicated login role per
# service: a compromised service can only reach its own database.
set -euo pipefail

create_service_db() {
  local db="$1" role="$2" password="$3"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
       -v db="$db" -v role="$role" -v password="$password" <<-'SQL'
	CREATE ROLE :"role" LOGIN PASSWORD :'password';
	CREATE DATABASE :"db" OWNER :"role";
	REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
	GRANT CONNECT, TEMPORARY ON DATABASE :"db" TO :"role";
SQL
  echo "created database $db owned by $role"
}

create_service_db "${AUTH_DB:-authdb}" "${AUTH_DB_USER:-auth_svc}" "${AUTH_DB_PASSWORD:?AUTH_DB_PASSWORD required}"
create_service_db "${USER_DB:-userdb}" "${USER_DB_USER:-user_svc}" "${USER_DB_PASSWORD:?USER_DB_PASSWORD required}"
create_service_db "${JOURNEY_DB:-journeydb}" "${JOURNEY_DB_USER:-journey_svc}" "${JOURNEY_DB_PASSWORD:?JOURNEY_DB_PASSWORD required}"
create_service_db "${SOCIAL_DB:-socialdb}" "${SOCIAL_DB_USER:-social_svc}" "${SOCIAL_DB_PASSWORD:?SOCIAL_DB_PASSWORD required}"

# PostGIS is not a trusted extension, so the superuser installs it for the services that
# need spatial queries (journey matching and filters).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "${JOURNEY_DB:-journeydb}" -c "CREATE EXTENSION IF NOT EXISTS postgis"
