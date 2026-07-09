#!/usr/bin/env bash
# Migrate the DB to head, then serve (spec §22: forward-only migrations in prod).
set -euo pipefail
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
