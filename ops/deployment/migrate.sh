#!/usr/bin/env bash
# Run exactly one controlled migration job after a verified backup and before release activation.
set -Eeuo pipefail

release=${1:?usage: migrate.sh STAGED_RELEASE BACKUP_DIR TRUSTED_BACKUP_PUBLIC_KEY}
backup_dir=${2:?usage: migrate.sh STAGED_RELEASE BACKUP_DIR TRUSTED_BACKUP_PUBLIC_KEY}
trusted_backup_key=${3:?usage: migrate.sh STAGED_RELEASE BACKUP_DIR TRUSTED_BACKUP_PUBLIC_KEY}
config_root=${MELD7T_CONFIG_ROOT:-$HOME/.config/meld7t}
[[ -f $release/release-receipt/images.lock ]] || { printf 'invalid staged release\n' >&2; exit 1; }
release_id=$(sed -n 's/^MELD7T_RELEASE_ID=//p' "$release/release-receipt/release.env")
[[ $release_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] \
  || { printf 'staged release ID is invalid\n' >&2; exit 1; }
staged_config="$config_root/releases/$release_id"
[[ -f $staged_config/env/api.env && -f $staged_config/trust/immudb-signing-public.pem ]] \
  || { printf 'release-scoped API configuration is not staged\n' >&2; exit 1; }
"$release/ops/deployment/verify-backup.sh" "$backup_dir" "$trusted_backup_key"
[[ -f $backup_dir/COMPLETE ]] || { printf 'verified backup lacks COMPLETE marker\n' >&2; exit 1; }
field() { sed -n "s/^$1=//p" "$backup_dir/backup.env"; }
[[ $(field MELD7T_BACKUP_FORMAT) == 2 ]] || { printf 'unsupported backup format\n' >&2; exit 1; }
backup_timestamp=$(field MELD7T_BACKUP_TIMESTAMP)
[[ $(<"$backup_dir/COMPLETE") == "$backup_timestamp" ]] \
  || { printf 'backup completion marker does not match signed timestamp\n' >&2; exit 1; }
if [[ $backup_timestamp =~ ^([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})([0-9]{2})Z$ ]]; then
  backup_epoch=$(date -u -d "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}-${BASH_REMATCH[3]} \
${BASH_REMATCH[4]}:${BASH_REMATCH[5]}:${BASH_REMATCH[6]} UTC" +%s)
else
  printf 'signed backup timestamp is malformed\n' >&2; exit 1
fi
age=$(( $(date -u +%s) - backup_epoch ))
((age >= -300 && age <= 86400)) \
  || { printf 'verified backup is outside the allowed 24-hour freshness window\n' >&2; exit 1; }
[[ $(field MELD7T_BACKUP_HOST) == "$(hostname -f 2>/dev/null || hostname)" ]] \
  || { printf 'verified backup belongs to a different host\n' >&2; exit 1; }
expected_release=$(readlink -f "${MELD7T_RELEASE_ROOT:-$HOME/.local/lib/meld7t}/current" 2>/dev/null \
  || readlink -f "$release")
[[ $(field MELD7T_BACKUP_RELEASE) == "$expected_release" ]] \
  || { printf 'backup does not bind the current host release\n' >&2; exit 1; }
systemctl --user stop caddy.service api.service meld7t-worker.service
api_image=$("$release/ops/release/image-lock.sh" --lock "$release/release-receipt/images.lock" get api)
podman run --rm --pull=never --name meld7t-schema-migration \
  --network meld-data-net \
  --env-file "$staged_config/env/api.env" \
  --volume api-immudb-state:/var/lib/meld7t/immudb-state:U \
  --volume "$staged_config/trust/immudb-signing-public.pem:/run/secrets/immudb-public-key.pem:ro,z" \
  --entrypoint /app/entrypoint.sh \
  "$api_image" migrate
podman run --rm --pull=never --name meld7t-profile-import \
  --network meld-data-net \
  --env-file "$staged_config/env/api.env" \
  --volume api-immudb-state:/var/lib/meld7t/immudb-state:U \
  --volume "$staged_config/trust/immudb-signing-public.pem:/run/secrets/immudb-public-key.pem:ro,z" \
  --volume "$release/harmonization:/data/harmonization:ro,z" \
  --entrypoint /app/entrypoint.sh \
  "$api_image" profiles
printf 'schema migration and signed active-profile import completed with %s\n' "$api_image"
