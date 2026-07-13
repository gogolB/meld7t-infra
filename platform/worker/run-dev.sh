#!/usr/bin/env bash
# Shared development/production launcher.  systemd supplies validated split env files in production;
# an interactive developer run may still source secrets/worker.env from the checkout.
set -Eeuo pipefail
umask 077

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"

if [[ ${MELD7T_SYSTEMD_ENV:-0} != 1 ]]; then
  dev_env=${MELD7T_WORKER_ENV_FILE:-$repo/secrets/worker.env}
  [[ -r $dev_env ]] || { printf 'worker environment is missing: %s\n' "$dev_env" >&2; exit 78; }
  set -a
  # This file is trusted operator configuration, never caller-controlled input.
  # shellcheck disable=SC1090
  source "$dev_env"
  set +a
fi

mode=${MELD7T_DEPLOYMENT_MODE:-development}
meld_data=${MELD7T_MELD_DATA:-$repo/meld-data}
staging=${MELD7T_DICOM_STAGING:-$meld_data/staging}
venv=${MELD7T_WORKER_VENV:-$here/.venv}

# The API container sees the logo at /run/branding; a host development worker needs the tracked
# source path instead. Production remains entirely controlled by its validated split environment.
if [[ $mode == development && -z ${MELD7T_BRANDING_LOGO_PATH:-} ]]; then
  export MELD7T_BRANDING_LOGO_PATH="$repo/containers/config/branding/report-logo.png"
fi

mkdir -p "$meld_data" "$staging"
chmod 0700 "$meld_data" "$staging"
fstype=$(findmnt -T "$staging" -n -o FSTYPE 2>/dev/null || true)
case "$fstype" in
  nfs|nfs4|cifs|smb3)
    printf 'active DICOM staging must be local NVMe, not %s: %s\n' "$fstype" "$staging" >&2
    exit 78
    ;;
esac

if [[ $mode == production || $mode == research ]]; then
  [[ ${MELD7T_RELEASE_MANIFEST_DIGEST:-} =~ ^(sha256:)?[0-9a-fA-F]{64}$ ]] || {
    printf 'server mode requires MELD7T_RELEASE_MANIFEST_DIGEST\n' >&2; exit 78; }
  for variable in MELD7T_PKG_IMAGE MELD7T_MELD_IMAGE MELD7T_HIPPUNFOLD_IMAGE MELD7T_MAP_IMAGE; do
    value=${!variable:-}
    [[ $value =~ @sha256:[0-9a-f]{64}$ ]] || {
      printf '%s must be a locked sha256 image reference\n' "$variable" >&2; exit 78; }
    podman image inspect "$value" >/dev/null 2>&1 || {
      printf 'locked image is not present locally for %s\n' "$variable" >&2; exit 78; }
  done
fi

[[ -x $venv/bin/arq ]] || { printf 'offline worker venv is absent: %s\n' "$venv" >&2; exit 78; }
export PYTHONPATH="${MELD7T_REPO_DIR:-$repo}/platform/api:${MELD7T_REPO_DIR:-$repo}/platform/worker"
exec "$venv/bin/arq" worker.tasks.WorkerSettings
