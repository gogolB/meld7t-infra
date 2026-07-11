#!/usr/bin/env bash
# Non-destructive media/crypto/archive drill.  Run the application-level restore test on an isolated
# host as the final scheduled DR step; this script never writes restored PHI to disk.
set -Eeuo pipefail

backup=${1:?usage: restore-drill.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY}
recipient_cert=${2:?usage: restore-drill.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY}
recipient_key=${3:?usage: restore-drill.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY}
trusted_key=${4:?usage: restore-drill.sh BACKUP_DIR RECIPIENT_CERT RECIPIENT_KEY TRUSTED_SIGNING_KEY}
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$script_dir/verify-backup.sh" "$backup" "$trusted_key"

decrypt() {
  openssl cms -decrypt -binary -inform DER -recip "$recipient_cert" -inkey "$recipient_key" -in "$1"
}

postgres_image=$("$script_dir/../release/image-lock.sh" --lock "$backup/images.lock" get postgres)
decrypt "$backup/meld.pgdump.cms" \
  | podman run --rm --pull=never --network=none --entrypoint pg_restore -i "$postgres_image" --list \
    >/dev/null
decrypt "$backup/orthanc-index.pgdump.cms" \
  | podman run --rm --pull=never --network=none --entrypoint pg_restore -i "$postgres_image" --list \
    >/dev/null
decrypt "$backup/postgres-globals.sql.cms" | grep -qE '^(CREATE|ALTER) ROLE'
for archive in orthanc-storage immudb-data api-immudb-state redis-data caddy-data hippunfold-cache \
  meld-data audit-state configuration; do
  decrypt "$backup/$archive.tar.cms" | tar -tf - >/dev/null
done
printf 'non-destructive restore drill passed: crypto, both PostgreSQL catalogs, and all archives\n'
printf 'next required step: restore this backup on an isolated acceptance host and run research workflow smoke tests\n'
