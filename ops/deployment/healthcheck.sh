#!/usr/bin/env bash
# Local, identifier-free production health gate suitable for a systemd timer or monitoring agent.
set -Eeuo pipefail

state_root=${MELD7T_STATE_ROOT:-$HOME/meld7t-state}
config_root=${MELD7T_CONFIG_ROOT:-$HOME/.config/meld7t}
backup_root=${MELD7T_BACKUP_ROOT:-}
backup_verify_key=${MELD7T_BACKUP_VERIFY_KEY:-}
max_backup_age_hours=${MELD7T_MAX_BACKUP_AGE_HOURS:-26}
failures=()
readonly -a units=(postgres redis immudb orthanc api ohif caddy meld7t-worker)

for unit in "${units[@]}"; do
  systemctl --user is-active --quiet "$unit.service" || failures+=("unit:$unit")
done

for container in postgres redis api caddy; do
  status=$(podman inspect "$container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' 2>/dev/null || true)
  [[ $status == healthy ]] || failures+=("health:$container:$status")
done

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
