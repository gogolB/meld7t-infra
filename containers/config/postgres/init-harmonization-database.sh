#!/bin/bash
# First-init only for the dedicated harmonization Orthanc database container.
set -euo pipefail

: "${ORTHANC__POSTGRESQL__PASSWORD:?ORTHANC__POSTGRESQL__PASSWORD is required}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" \
  --set=orthanc_password="$ORTHANC__POSTGRESQL__PASSWORD" <<'SQL'
CREATE ROLE orthanc LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  PASSWORD :'orthanc_password';
CREATE DATABASE harmonization_orthanc OWNER orthanc;

REVOKE CONNECT ON DATABASE harmonization_orthanc FROM PUBLIC;
GRANT CONNECT ON DATABASE harmonization_orthanc TO orthanc;
SQL
