#!/usr/bin/env bash
# Quiesced, encrypted backup of all research/application state.  No PHI is written in plaintext.
set -Eeuo pipefail
umask 077

dest_parent=${1:?usage: backup.sh DEST_PARENT RECIPIENT_CERT SIGNING_KEY [RELEASE_DIR]}
recipient_cert=${2:?usage: backup.sh DEST_PARENT RECIPIENT_CERT SIGNING_KEY [RELEASE_DIR]}
signing_key=${3:?usage: backup.sh DEST_PARENT RECIPIENT_CERT SIGNING_KEY [RELEASE_DIR]}
release_source=${4:-$HOME/.local/lib/meld7t/current}
config_root=${MELD7T_CONFIG_ROOT:-$HOME/.config/meld7t/current}
meld_data=${MELD7T_MELD_DATA:-$HOME/meld7t-state/meld-data}
audit_state=${MELD7T_AUDIT_STATE:-$HOME/meld7t-state/audit}
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$dest_parent/meld7t-backup-$timestamp"
readonly -a quiesce_units=(caddy.service api.service meld7t-worker.service orthanc.service redis.service immudb.service)
declare -a restart_units=()

die() { printf 'backup: %s\n' "$*" >&2; exit 1; }
for file in "$recipient_cert" "$signing_key"; do [[ -f $file ]] || die "missing key material: $file"; done
config_real=$(readlink -f "$config_root" 2>/dev/null || true)
[[ -d $config_real && -d $meld_data && -d $audit_state ]] \
  || die "config, MELD data, or audit state directory is absent"
[[ ! -e $dest ]] || die "backup destination already exists: $dest"
mkdir -p "$dest"

restart_services() {
  if ((${#restart_units[@]})); then
    systemctl --user start "${restart_units[@]}" || true
  fi
}
trap restart_services EXIT

for unit in "${quiesce_units[@]}"; do
  if systemctl --user is-active --quiet "$unit"; then restart_units+=("$unit"); fi
done
if ((${#restart_units[@]})); then systemctl --user stop "${restart_units[@]}"; fi
systemctl --user start postgres.service
systemctl --user is-active --quiet postgres.service || die "Postgres did not become ready"

encrypt() {
  local output=$1
  openssl cms -encrypt -binary -stream -aes-256-cbc -outform DER \
    -recip "$recipient_cert" -out "$dest/$output"
}

podman exec postgres pg_dumpall --globals-only --no-role-passwords -U postgres \
  | encrypt postgres-globals.sql.cms
podman exec postgres pg_dump --format=custom --compress=9 --no-owner -U postgres -d meld \
  | encrypt meld.pgdump.cms
podman exec postgres pg_dump --format=custom --compress=9 --no-owner -U postgres -d orthanc \
  | encrypt orthanc-index.pgdump.cms

for volume in orthanc-storage immudb-data api-immudb-state redis-data caddy-data hippunfold-cache; do
  podman volume exists "$volume" || die "required volume is absent: $volume"
  podman volume export "$volume" | encrypt "$volume.tar.cms"
done
tar -C "$(dirname "$meld_data")" --one-file-system -cf - "$(basename "$meld_data")" \
  | encrypt meld-data.tar.cms
tar -C "$(dirname "$audit_state")" --one-file-system -cf - "$(basename "$audit_state")" \
  | encrypt audit-state.tar.cms
tar -C "$(dirname "$config_real")" --one-file-system -cf - "$(basename "$config_real")" \
  | encrypt configuration.tar.cms

release_target=$(readlink -f "$release_source" 2>/dev/null || printf unknown)
[[ -f $release_target/release-receipt/images.lock ]] \
  || die "active release image lock is unavailable: $release_target"
cp "$release_target/release-receipt/images.lock" "$dest/images.lock"
recipient_sha256=$(openssl x509 -in "$recipient_cert" -outform DER | sha256sum | awk '{print $1}')
cat >"$dest/backup.env" <<EOF
MELD7T_BACKUP_FORMAT=2
MELD7T_BACKUP_TIMESTAMP=$timestamp
MELD7T_BACKUP_HOST=$(hostname -f 2>/dev/null || hostname)
MELD7T_BACKUP_RELEASE=$release_target
MELD7T_BACKUP_RECIPIENT_SHA256=$recipient_sha256
EOF
printf '%s\n' "$timestamp" >"$dest/COMPLETE"
(cd "$dest" && find . -type f ! -name SHA256SUMS ! -name SHA256SUMS.sig \
  -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
openssl dgst -sha256 -sign "$signing_key" -out "$dest/SHA256SUMS.sig" "$dest/SHA256SUMS"
chmod -R a-w "$dest"
trap - EXIT
restart_services
printf 'encrypted backup complete: %s\n' "$dest"
printf '%s/COMPLETE\n' "$dest"
