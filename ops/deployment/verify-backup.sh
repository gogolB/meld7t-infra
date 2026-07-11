#!/usr/bin/env bash
set -Eeuo pipefail

backup=${1:?usage: verify-backup.sh BACKUP_DIR TRUSTED_SIGNING_PUBLIC_KEY}
trusted_key=${2:?usage: verify-backup.sh BACKUP_DIR TRUSTED_SIGNING_PUBLIC_KEY}
die() { printf 'verify-backup: %s\n' "$*" >&2; exit 1; }
[[ -f $backup/COMPLETE && -f $backup/backup.env && -f $backup/SHA256SUMS \
   && -f $backup/SHA256SUMS.sig && -f $backup/images.lock && -f $trusted_key ]] \
  || die "backup is incomplete"
grep -Fxq './COMPLETE' <(cut -d' ' -f3- "$backup/SHA256SUMS") \
  || die "completion marker is not covered by the signed checksum manifest"
openssl dgst -sha256 -verify "$trusted_key" -signature "$backup/SHA256SUMS.sig" \
  "$backup/SHA256SUMS" >/dev/null || die "backup signature is invalid"
(cd "$backup" && sha256sum --strict --check SHA256SUMS >/dev/null) \
  || die "backup checksum verification failed"
for encrypted in "$backup"/*.cms; do
  openssl cms -cmsout -inform DER -in "$encrypted" -noout >/dev/null \
    || die "invalid CMS envelope: $(basename "$encrypted")"
done
printf 'backup signature, checksums, and encrypted envelopes verified: %s\n' "$backup"
