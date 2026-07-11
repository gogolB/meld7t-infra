#!/usr/bin/env bash
# Serving and schema migration are separate production lifecycle operations.
set -Eeuo pipefail
umask 027

mode=${1:-serve}
case "$mode" in
  migrate)
    exec alembic upgrade head
    ;;
  check-schema)
    exec alembic current --check-heads
    ;;
  profiles)
    exec python -m app.profile_import
    ;;
  serve)
    auto_migrate=${MELD7T_AUTO_MIGRATE:-}
    if [[ -z $auto_migrate ]]; then
      [[ ${MELD7T_DEPLOYMENT_MODE:-development} == production ]] \
        && auto_migrate=false || auto_migrate=true
    fi
    if [[ $auto_migrate == true ]]; then
      if [[ ${MELD7T_DEPLOYMENT_MODE:-development} == production ]]; then
        printf 'MELD7T_AUTO_MIGRATE is forbidden in production; run the controlled migration job\n' >&2
        exit 78
      fi
      alembic upgrade head
    fi
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port 8000 \
      --no-proxy-headers \
      --timeout-keep-alive "${MELD7T_HTTP_KEEPALIVE_SECONDS:-10}" \
      --limit-concurrency "${MELD7T_HTTP_MAX_CONCURRENCY:-200}"
    ;;
  *)
    printf 'usage: entrypoint.sh {serve|migrate|check-schema|profiles}\n' >&2
    exit 64
    ;;
esac
