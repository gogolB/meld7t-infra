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

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<SQL
CREATE ROLE orthanc LOGIN PASSWORD '${ORTHANC__POSTGRESQL__PASSWORD}';
CREATE DATABASE orthanc OWNER orthanc;

CREATE ROLE meld LOGIN PASSWORD '${MELD_DB_PASSWORD}';
CREATE DATABASE meld OWNER meld;
SQL
