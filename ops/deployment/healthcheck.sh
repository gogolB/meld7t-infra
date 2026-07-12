#!/usr/bin/env bash
# Local, identifier-free production health gate suitable for a systemd timer or monitoring agent.
set -Eeuo pipefail

state_root=${MELD7T_STATE_ROOT:-$HOME/meld7t-state}
config_root=${MELD7T_CONFIG_ROOT:-$HOME/.config/meld7t}
backup_root=${MELD7T_BACKUP_ROOT:-}
backup_verify_key=${MELD7T_BACKUP_VERIFY_KEY:-}
max_backup_age_hours=${MELD7T_MAX_BACKUP_AGE_HOURS:-26}
harmonization_orthanc_max_used_percent=${MELD7T_HARMONIZATION_ORTHANC_MAX_USED_PERCENT:-85}
failures=()
readonly -a units=(postgres redis immudb orthanc harmonization-postgres harmonization-orthanc \
  api ohif caddy meld7t-worker meld7t-harmonization-builder)

for unit in "${units[@]}"; do
  systemctl --user is-active --quiet "$unit.service" || failures+=("unit:$unit")
done

for container in postgres redis harmonization-postgres harmonization-orthanc api caddy; do
  status=$(podman inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' 2>/dev/null || true)
  [[ $status == healthy ]] || failures+=("health:$container:$status")
done

# Orthanc's configured cap is independent of filesystem capacity. Alert before its fail-closed
# Reject mode starts refusing controls, even when the underlying Podman filesystem is mostly empty.
if [[ $harmonization_orthanc_max_used_percent =~ ^[0-9]+$ ]] \
   && ((harmonization_orthanc_max_used_percent >= 1 \
        && harmonization_orthanc_max_used_percent <= 99)); then
  orthanc_quota_status=
  if ! orthanc_quota_status=$(podman exec harmonization-orthanc python3 -c '
import base64, json, os, sys, urllib.request

threshold = int(sys.argv[1])
cap_mib = int(os.environ["ORTHANC__MAXIMUM_STORAGE_SIZE"])
users = json.loads(os.environ["ORTHANC__REGISTERED_USERS"])
password = users["harmonization-builder"]
token = base64.b64encode(("harmonization-builder:" + password).encode()).decode()
request = urllib.request.Request(
    "http://127.0.0.1:8042/statistics",
    headers={"Authorization": "Basic " + token},
)
with urllib.request.urlopen(request, timeout=5) as response:
    statistics = json.load(response)
used_mib = int(float(statistics["TotalDiskSizeMB"]))
if cap_mib <= 0 or used_mib < 0:
    raise ValueError("invalid Orthanc storage statistics")
used_percent = min(100, (used_mib * 100) // cap_mib)
print(f"used-{used_percent}-percent-of-cap")
raise SystemExit(2 if used_mib * 100 >= cap_mib * threshold else 0)
' "$harmonization_orthanc_max_used_percent" 2>/dev/null); then
    failures+=("storage:harmonization-orthanc:${orthanc_quota_status:-unavailable}")
  fi
else
  failures+=("config:harmonization-orthanc-max-used-percent")
fi

podman exec api python -c \
  'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/readyz", timeout=10).read()' \
  >/dev/null 2>&1 || failures+=("readiness:api")
nvidia-smi --query-gpu=uuid --format=csv,noheader >/dev/null 2>&1 || failures+=("gpu")
openssl x509 -checkend 2592000 -noout -in "$config_root/tls/tls.crt" >/dev/null 2>&1 \
  || failures+=("tls:expires-within-30-days")

for path in "$state_root" "$HOME/.local/share/containers"; do
  used=$(df --output=pcent "$path" 2>/dev/null | tail -1 | tr -dc '0-9')
  [[ -n $used && $used -lt 85 ]] || failures+=("disk:${path}:used-${used:-unknown}-percent")
done

if [[ -n $backup_root ]]; then
  latest=$(timeout 10 find "$backup_root" -mindepth 1 -maxdepth 1 -type d \
    -name 'meld7t-backup-*' -printf '%f\n' 2>/dev/null | sort -r | head -1)
  if [[ -z $latest ]]; then
    failures+=("backup:missing")
  elif [[ -z $backup_verify_key ]] \
       || ! "${MELD7T_RELEASE_ROOT:-$HOME/.local/lib/meld7t}/current/ops/deployment/verify-backup.sh" \
         "$backup_root/$latest" "$backup_verify_key" >/dev/null 2>&1; then
    failures+=("backup:unverified")
  else
    signed_timestamp=$(sed -n 's/^MELD7T_BACKUP_TIMESTAMP=//p' "$backup_root/$latest/backup.env")
    if [[ $signed_timestamp =~ ^([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z$ ]]; then
      signed_epoch=$(date -u -d "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]} \
${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:${BASH_REMATCH[6]} UTC" +%s)
      age=$(( $(date -u +%s) - signed_epoch ))
      ((age >= -300 && age <= max_backup_age_hours * 3600)) || failures+=("backup:stale")
    else
      failures+=("backup:timestamp")
    fi
  fi
fi

timestamp=$(date -u +%FT%TZ)
if ((${#failures[@]})); then
  printf '{"timestamp":"%s","status":"failed","checks":"%s"}\n' \
    "$timestamp" "$(IFS=,; printf '%s' "${failures[*]}")"
  exit 1
fi
printf '{"timestamp":"%s","status":"ok"}\n' "$timestamp"
