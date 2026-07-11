#!/bin/bash
# First-init only (runs when postgres-data is empty): create the two databases + roles the
# platform needs (spec §2.1, §4, §8):
#   orthanc — Orthanc's DICOM index (Orthanc PostgreSQL plugin)
#   meld    — application results/metadata DB (FastAPI/SQLModel)
# Role passwords come from the container environment (services.env); never hard-coded here.
# ORTHANC__POSTGRESQL__PASSWORD is the same value the orthanc container injects into its
# plugin config, so the role and the plugin agree by construction.
set -euo pipefail

: "${ORTHANC__POSTGRESQL__PASSWORD:?ORTHANC__POSTGRESQL__PASSWORD is required}"
: "${MELD_DB_PASSWORD:?MELD_DB_PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" \
  --set=orthanc_password="$ORTHANC__POSTGRESQL__PASSWORD" \
  --set=meld_password="$MELD_DB_PASSWORD" <<'SQL'
CREATE ROLE orthanc LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'orthanc_password';
CREATE DATABASE orthanc OWNER orthanc;

CREATE ROLE meld LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'meld_password';
CREATE DATABASE meld OWNER meld;

REVOKE CONNECT ON DATABASE orthanc FROM PUBLIC;
GRANT CONNECT ON DATABASE orthanc TO orthanc;
REVOKE CONNECT ON DATABASE meld FROM PUBLIC;
GRANT CONNECT ON DATABASE meld TO meld;
SQL
