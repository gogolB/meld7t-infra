#!/usr/bin/env bash
# Dev launcher for the host worker (§2.3). Prod runs this via a systemd user unit.
# Loads loopback env from secrets/worker.env, puts the shared `app` package on PYTHONPATH,
# and starts the Arq worker (max_jobs=1 → GPU-serialized).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
set -a; source "$repo/secrets/worker.env"; set +a
export PYTHONPATH="$repo/platform/api:$repo/platform/worker"
exec "$here/.venv/bin/arq" worker.tasks.WorkerSettings
