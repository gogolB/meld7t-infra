#!/usr/bin/env bash
# Restore non-Postgres named volumes into an empty, isolated target namespace for a full DR drill.
# It deliberately does not switch live Quadlets or overwrite production volumes.
set -Eeuo pipefail

backup=${1:?usage: restore-volumes.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY PREFIX --confirm-isolated}
recipient_cert=${2:?usage: restore-volumes.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY PREFIX --confirm-isolated}
recipient_key=${3:?usage: restore-volumes.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY PREFIX --confirm-isolated}
trusted_key=${4:?usage: restore-volumes.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY PREFIX --confirm-isolated}
prefix=${5:?usage: restore-volumes.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY PREFIX --confirm-isolated}
confirmation=${6:-}
[[ $confirmation == --confirm-isolated ]] || { printf 'missing --confirm-isolated\n' >&2; exit 64; }
[[ $prefix =~ ^drill-[a-z0-9][a-z0-9-]{0,31}$ ]] || {
  printf 'prefix must begin with drill- and contain lowercase letters/digits/hyphens\n' >&2; exit 64; }
"$(dirname "$0")/verify-backup.sh" "$backup" "$trusted_key"

created=()
complete=false
cleanup() {
  if ! $complete && ((${#created[@]})); then
    podman volume rm "${created[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

for volume in orthanc-storage immudb-data api-immudb-state redis-data caddy-data hippunfold-cache; do
  target="$prefix-$volume"
  if podman volume exists "$target"; then
    printf 'refusing to replace existing drill volume: %s\n' "$target" >&2
    exit 1
  fi
  podman volume create "$target" >/dev/null
  created+=("$target")
  openssl cms -decrypt -binary -inform DER -recip "$recipient_cert" -inkey "$recipient_key" \
    -in "$backup/$volume.tar.cms" | podman volume import "$target" -
  printf 'restored isolated volume %s\n' "$target"
done
complete=true
trap - EXIT
